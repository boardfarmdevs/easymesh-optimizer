import json

import pytest

from room_demo.trace import TraceSink
from room_demo.trace_report import journal_events, matched_packet, statistics, timestamp


def test_journal_reader_rejects_tampering_and_expired_history(tmp_path):
    sink = TraceSink(tmp_path)
    sink.emit("trace.started", {})
    sink.emit("trace.finished", {})
    sink.journal.close()
    assert len(list(journal_events(tmp_path))) == 2
    path = tmp_path / "live-events.jsonl"
    path.write_text(path.read_text().replace("trace.finished", "trace.modified"))
    with pytest.raises(ValueError, match="invalid event chain"):
        list(journal_events(tmp_path))
    index = json.loads((tmp_path / "journal-index.json").read_text())
    index["retained_from_sequence"] = 2
    (tmp_path / "journal-index.json").write_text(json.dumps(index))
    with pytest.raises(ValueError, match="beginning-to-end"):
        list(journal_events(tmp_path))


def test_packet_match_requires_clock_window_and_dialog_identity():
    packets = [{"at": 1, "token": "1", "station": "one"}, {"at": 2, "token": "2", "station": "one"},
               {"at": 3, "token": "1", "station": "two"}]
    assert matched_packet(packets, 1.5, 3, {"token": "1", "station": "one"}) is None
    assert matched_packet(packets, 1.5, 3, {"token": "2", "station": "one"}) == packets[1]
    assert statistics([None, 5, 2, 9]) == {"count": 3, "median": 5, "p95": 9, "maximum": 9}


def test_nanosecond_observer_timestamps_work_on_python_310():
    assert timestamp("2026-09-08T17:43:29.335816716Z") == timestamp("2026-09-08T17:43:29.335816+00:00")


def test_journal_reader_rejects_a_valid_but_truncated_tail(tmp_path):
    sink = TraceSink(tmp_path)
    sink.emit("trace.started", {})
    sink.emit("trace.finished", {})
    sink.journal.close()
    path = tmp_path / "live-events.jsonl"
    path.write_text(path.read_text().splitlines(keepends=True)[0])
    with pytest.raises(ValueError, match="segment is incomplete"):
        list(journal_events(tmp_path))


def test_journal_reader_rejects_failed_shutdown_even_with_intact_segments(tmp_path):
    sink = TraceSink(tmp_path)
    sink.emit("trace.started", {})
    sink.journal.close()
    (tmp_path / "storage-summary.json").write_text(json.dumps({"journal": {
        "complete": False, "queued_bytes": 0, "written_sequence": 1}}))
    with pytest.raises(ValueError, match="shutdown was incomplete"):
        list(journal_events(tmp_path))
