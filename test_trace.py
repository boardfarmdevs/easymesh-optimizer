import json

import pytest

from room_demo.trace import TraceSink, take_lines


def test_control_stream_split_and_prompt():
    pending, lines = take_lines(b"> sta", b"tus\r\nbssid=02:00:00:00:00:01\nCTRL-EVENT-CON")
    assert lines == ["status", "bssid=02:00:00:00:00:01"]
    pending, lines = take_lines(pending, b"NECTED\n> ")
    assert lines == ["CTRL-EVENT-CONNECTED"]
    assert pending == b"> "


def test_control_stream_bound():
    with pytest.raises(ValueError, match="bounded"):
        take_lines(b"x" * 65536, b"x")


def test_trace_chain_and_storage(tmp_path):
    sink = TraceSink(tmp_path)
    sink.emit("trace.started", {})
    sink.emit("trace.client", {"line": "CTRL-EVENT-CONNECTED"})
    sink.journal.close()
    events = [json.loads(line) for line in (tmp_path / "live-events.jsonl").read_text().splitlines()]
    assert events[0]["previous_event_hash"] is None
    assert events[1]["previous_event_hash"] == events[0]["event_hash"]
    assert events[1]["receipt_monotonic_ns"] >= events[0]["receipt_monotonic_ns"]
    assert sink.journal.status()["complete"]
