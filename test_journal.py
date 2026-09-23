import json
from pathlib import Path
import threading
import time
from unittest.mock import patch

import pytest

from room_demo.events import EventHistoryGap, EventStore
from room_demo.journal import BoundedJournal


WORLD = {"name": "bounded", "duration_ms": 1000, "tick_ms": 100}


def test_live_history_is_bounded_without_breaking_hash_chain(tmp_path):
    store = EventStore("run", WORLD, tmp_path / "events.jsonl", history_events=3)
    events = [store.emit("scenario.clock", sequence, {}) for sequence in range(12)]
    assert [event["sequence"] for event in store.all()] == [10, 11, 12]
    assert events[-1]["previous_event_hash"] == events[-2]["event_hash"]
    assert store.current()["sequence"] == 12
    assert store.storage_status()["history"]["discarded_events"] == 9
    with pytest.raises(EventHistoryGap):
        store.after(1)
    assert store.after(11) == [events[-1]]


def test_byte_limit_handles_one_event_larger_than_replay_window(tmp_path):
    store = EventStore("run", WORLD, tmp_path / "events.jsonl", history_bytes=100)
    first = store.emit("scenario.clock", 0, {})
    second = store.emit("scenario.clock", 0, {})
    assert store.all() == []
    assert second["previous_event_hash"] == first["event_hash"]
    assert store.storage_status()["history"]["retained_bytes"] == 0
    with pytest.raises(EventHistoryGap):
        store.wait_after(0, 0)


def test_async_journal_closes_with_every_accepted_event(tmp_path):
    path = tmp_path / "live-events.jsonl"
    store = EventStore("run", WORLD, path, asynchronous=True)
    for sequence in range(100):
        store.emit("scenario.clock", sequence, {})
    store.close()
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert [event["sequence"] for event in events] == list(range(1, 101))
    assert store.storage_status()["journal"]["complete"]
    assert not store.storage_status()["journal"]["writer_alive"]
    assert json.loads((tmp_path / "journal-index.json").read_text())["complete"]


def test_rotating_disk_limit_is_explicit_and_refuses_incomplete_full_replay(tmp_path):
    store = EventStore("run", WORLD, tmp_path / "live-events.jsonl", asynchronous=True,
                       journal_options={"segment_bytes": 2048, "segments": 2})
    for sequence in range(80):
        store.emit("scenario.clock", sequence, {})
    store.close()
    files = list(tmp_path.glob("live-events*.jsonl"))
    assert len(files) == 2
    assert sum(path.stat().st_size for path in files) <= 4096
    status = store.storage_status()["journal"]
    assert status["history_truncated"] and status["complete"]
    assert status["written_sequence"] == 80
    (tmp_path / "world.json").write_text(json.dumps(WORLD))
    with pytest.raises(ValueError, match="beginning has expired"):
        EventStore.from_evidence(tmp_path)


def test_rotated_complete_evidence_replays_all_segments(tmp_path):
    store = EventStore("run", WORLD, tmp_path / "live-events.jsonl", asynchronous=True,
                       journal_options={"segment_bytes": 2048, "segments": 20})
    for sequence in range(30):
        store.emit("scenario.clock", sequence, {})
    store.close()
    (tmp_path / "world.json").write_text(json.dumps(WORLD))
    replay = EventStore.from_evidence(tmp_path)
    assert len(replay.all()) == 30
    assert replay.current()["evidence_digest"] == store.current()["evidence_digest"]


def test_stalled_writer_never_blocks_control_and_exposes_queue_overflow(tmp_path):
    gate = threading.Event()
    with patch.object(BoundedJournal, "_run", lambda _self: gate.wait(5)):
        store = EventStore("run", WORLD, tmp_path / "live-events.jsonl", asynchronous=True,
                           journal_options={"queue_bytes": 1024})
        try:
            started = time.monotonic()
            for sequence in range(30):
                store.emit("scenario.clock", sequence, {})
            assert time.monotonic() - started < 1
            assert store.current()["sequence"] == 30
            status = store.storage_status()["journal"]
            assert not status["complete"]
            assert status["queued_bytes"] <= 1024
            assert "queue capacity" in status["error"]
        finally:
            gate.set()
            store.close()


def test_existing_evidence_is_not_overwritten(tmp_path):
    path = tmp_path / "live-events.jsonl"
    path.write_text("existing evidence\n")
    store = EventStore("run", WORLD, path, asynchronous=True)
    store.emit("scenario.clock", 0, {})
    store.close()
    assert path.read_text() == "existing evidence\n"
    assert not store.storage_status()["journal"]["complete"]


def test_completed_movement_history_is_bounded(tmp_path):
    store = EventStore("run", WORLD, tmp_path / "events.jsonl", persist=False)
    for sequence in range(160):
        store.emit("interaction.movement.completed", 0,
                   {"movement": {"id": str(sequence), "role": "client", "status": "completed"}})
    assert len(store.current()["movements"]) == 128
