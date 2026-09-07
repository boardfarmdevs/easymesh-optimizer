from __future__ import annotations

import json

import pytest

from optimizer.recorder import Journal, read_records, verify_records


def test_journal_is_append_only_and_hash_chained(tmp_path):
    path = tmp_path / "experiment.jsonl"
    journal = Journal(path)
    journal.append("snapshot", {"value": 1}, recorded_at="2026-08-20T20:00:00.000Z")
    journal.append("evaluation", {"value": 2}, recorded_at="2026-08-20T20:00:01.000Z")
    records = list(read_records(path))
    verify_records(records)
    assert records[1]["previous_hash"] == records[0]["record_hash"]

    records[0]["payload"]["value"] = 99
    path.write_text("\n".join(json.dumps(item) for item in records) + "\n")
    with pytest.raises(ValueError, match="record hash"):
        Journal(path)
