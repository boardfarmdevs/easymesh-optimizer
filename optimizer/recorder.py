from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

from .model import format_time


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class Journal:
    """Append-only, hash-chained JSON-lines experiment journal."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sequence = 0
        self.previous_hash = "0" * 64
        if self.path.exists():
            records = list(read_records(self.path))
            verify_records(records)
            if records:
                self.sequence = records[-1]["sequence"] + 1
                self.previous_hash = records[-1]["record_hash"]

    def append(self, kind: str, payload: Any, *, recorded_at: str | None = None) -> dict[str, Any]:
        record = {
            "schema_version": 1,
            "sequence": self.sequence,
            "recorded_at": recorded_at or format_time(datetime.now(timezone.utc)),
            "kind": kind,
            "previous_hash": self.previous_hash,
            "payload": payload,
        }
        record["record_hash"] = hashlib.sha256(_canonical(record)).hexdigest()
        with self.path.open("ab") as output:
            output.write(_canonical(record) + b"\n")
            output.flush()
            os.fsync(output.fileno())
        self.sequence += 1
        self.previous_hash = record["record_hash"]
        return record


def read_records(path: str | Path) -> Iterable[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(f"invalid journal JSON on line {number}") from error


def verify_records(records: Iterable[dict[str, Any]]) -> None:
    previous = "0" * 64
    expected_sequence = 0
    for record in records:
        if record["sequence"] != expected_sequence:
            raise ValueError("journal sequence is not contiguous")
        if record["previous_hash"] != previous:
            raise ValueError("journal hash chain is broken")
        claimed = record["record_hash"]
        unsigned = dict(record)
        del unsigned["record_hash"]
        actual = hashlib.sha256(_canonical(unsigned)).hexdigest()
        if claimed != actual:
            raise ValueError("journal record hash is invalid")
        previous = claimed
        expected_sequence += 1
