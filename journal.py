from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import threading
import time


class BoundedJournal:
    """Ordered asynchronous evidence, bounded independently of control workers."""

    def __init__(self, path: Path, *, segment_bytes=64 * 1024 * 1024,
                 segments=8, queue_bytes=16 * 1024 * 1024):
        if min(segment_bytes, segments, queue_bytes) < 1:
            raise ValueError("journal bounds must be positive")
        self.path = path
        self.segment_bytes = segment_bytes
        self.segments = segments
        self.queue_bytes = queue_bytes
        self._condition = threading.Condition()
        self._pending = deque()
        self._pending_bytes = 0
        self._closed = False
        self._error = None
        self._written_sequence = 0
        self._written_bytes = 0
        self._retained_from = 1
        self._retained = []
        self._thread = threading.Thread(target=self._run, name="room-evidence-writer", daemon=True)
        self._thread.start()

    def append(self, event: dict, encoded: bytes) -> None:
        with self._condition:
            if self._closed or self._error:
                return
            if len(encoded) > self.segment_bytes or self._pending_bytes + len(encoded) > self.queue_bytes:
                self._error = "evidence queue capacity exceeded; control continues but evidence is incomplete"
                self._condition.notify_all()
                return
            self._pending.append((event["sequence"], event.get("previous_event_hash"),
                                  event.get("event_hash"), encoded))
            self._pending_bytes += len(encoded)
            self._condition.notify_all()

    def status(self) -> dict:
        with self._condition:
            return {"mode": "bounded-async", "complete": self._error is None,
                    "error": self._error, "written_sequence": self._written_sequence,
                    "written_bytes": self._written_bytes, "queued_bytes": self._pending_bytes,
                    "maximum_queued_bytes": self.queue_bytes,
                    "maximum_retained_bytes": self.segment_bytes * self.segments,
                    "retained_from_sequence": self._retained_from,
                    "history_truncated": self._retained_from > 1,
                    "writer_alive": self._thread.is_alive()}

    def flush(self, sequence: int, timeout=5) -> bool:
        with self._condition:
            self._condition.wait_for(lambda: self._written_sequence >= sequence or self._error is not None,
                                     timeout=timeout)
            return self._written_sequence >= sequence and self._error is None

    def close(self, timeout=5) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._thread.join(timeout)
        if self._thread.is_alive():
            with self._condition:
                self._error = "evidence writer did not drain before shutdown deadline"

    def _index(self, current: dict) -> None:
        document = {"schema": "easymesh.room-demo.journal.v1", "segments": self._retained + [current],
                    "retained_from_sequence": self._retained_from, "complete": self._error is None,
                    "error": self._error}
        temporary = self.path.with_name("journal-index.json.tmp")
        temporary.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.path.with_name("journal-index.json"))

    def _run(self) -> None:
        stream = None
        current = {"file": self.path.name, "bytes": 0, "first_sequence": None, "last_sequence": 0,
                   "previous_event_hash": None, "last_event_hash": None}
        segment = 0
        indexed_at = 0.0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            stream = self.path.open("xb", buffering=0)
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._pending or self._closed or self._error)
                    if not self._pending:
                        break
                    sequence, previous_hash, event_hash, encoded = self._pending.popleft()
                if current["bytes"] and current["bytes"] + len(encoded) > self.segment_bytes:
                    stream.close()
                    segment += 1
                    archived = self.path.with_name(f"live-events.{segment:06d}.jsonl")
                    self.path.replace(archived)
                    self._retained.append({**current, "file": archived.name})
                    while len(self._retained) >= self.segments:
                        expired = self._retained.pop(0)
                        self.path.with_name(expired["file"]).unlink()
                    current = {"file": self.path.name, "bytes": 0, "first_sequence": None,
                               "last_sequence": 0, "previous_event_hash": None, "last_event_hash": None}
                    stream = self.path.open("xb", buffering=0)
                    indexed_at = 0.0
                if current["first_sequence"] is None:
                    current["first_sequence"] = sequence
                    current["previous_event_hash"] = previous_hash
                if stream.write(encoded) != len(encoded):
                    raise OSError("short evidence write")
                current.update(bytes=current["bytes"] + len(encoded), last_sequence=sequence,
                               last_event_hash=event_hash)
                with self._condition:
                    self._pending_bytes -= len(encoded)
                    self._written_bytes += len(encoded)
                    self._written_sequence = sequence
                    self._retained_from = (self._retained[0] if self._retained else current)["first_sequence"]
                    self._condition.notify_all()
                if time.monotonic() - indexed_at >= 1:
                    self._index(current)
                    indexed_at = time.monotonic()
            self._index(current)
        except Exception as error:
            with self._condition:
                self._error = f"evidence writer failed: {error}"
                self._pending.clear()
                self._pending_bytes = 0
                self._condition.notify_all()
        finally:
            if stream is not None:
                stream.close()
