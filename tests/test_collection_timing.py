from datetime import datetime, timezone

import pytest

from optimizer import collection_timing
from optimizer.observer import ControllerObserver


def test_transport_duration_uses_monotonic_time_and_preserves_failure(monkeypatch):
    marks = iter(({"at": 100, "monotonic_ns": 1000000000},
                  {"at": 90, "monotonic_ns": 1250000000}))
    monkeypatch.setattr(collection_timing, "timing_mark", lambda: next(marks))
    failure = OSError("transport closed")
    timings = {}

    def fail():
        raise failure

    with pytest.raises(OSError) as caught:
        collection_timing.timed_call(timings, "clients", fail)
    assert caught.value is failure
    assert timings["clients"]["elapsed_ms"] == 250
    assert timings["clients"]["finished"]["at"] == 90
    assert timings["clients"]["error_type"] == "OSError"


def test_passive_request_brackets_precede_candidates_without_relabeling_metrics(monkeypatch):
    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    marks = iter({"at": index, "monotonic_ns": index * 1000000000} for index in range(8))
    monkeypatch.setattr(collection_timing, "timing_mark", lambda: next(marks))
    payloads = {"topology": {"nodes": []}, "devices": {"devices": []}, "bsses": {"bsses": []},
                "clients": {"clients": [
                    {"mac": "02:00:00:00:03:00", "connected_bssid": "02:00:00:aa:aa:01",
                     "client_metrics": {"rcpi": 144, "last_updated": "2026-09-14T00:00:00Z"}},
                    {"mac": "02:00:00:00:04:00", "connected_bssid": "02:00:00:aa:aa:01",
                     "client_metrics": {"rcpi": 0, "last_updated": None}}]}}

    def candidates(*arguments):
        assert observer.last_raw["api_timings"]["clients"]["finished"]["at"] == 3
        assert observer.last_raw["api_timings"]["bsses"]["finished"]["at"] == 7
        return []

    observer = ControllerObserver(fetcher=lambda url: payloads[url.rsplit("/", 1)[-1]],
                                  candidate_provider=candidates, clock=lambda: now)
    snapshot = observer.observe()
    assert list(observer.last_raw["api_timings"]) == ["topology", "clients", "devices", "bsses"]
    assert all(row["elapsed_ms"] == 1000 for row in observer.last_raw["api_timings"].values())
    assert snapshot.clients[0].metric_observed_at == "2026-09-14T00:00:00Z"
    assert snapshot.clients[0].rcpi == 144
    assert snapshot.clients[1].metric_observed_at is None
    assert snapshot.clients[1].rcpi is None
    assert observer.last_raw["clients"] is payloads["clients"]


def test_failed_passive_read_retains_partial_evidence_without_previous_cycle():
    observer = ControllerObserver(fetcher=lambda url: {})
    observer.observe()
    previous = observer.last_raw

    def fetch(url):
        if url.endswith("/clients"):
            raise OSError("controller unavailable")
        return {"nodes": []}

    observer.fetcher = fetch
    with pytest.raises(OSError, match="controller unavailable"):
        observer.observe()
    assert observer.last_raw is not previous
    assert observer.last_raw["topology"] == {"nodes": []}
    assert "clients" not in observer.last_raw
    assert "devices" not in observer.last_raw
    assert "sampled_at" not in observer.last_raw
    assert observer.last_raw["api_timings"]["clients"]["error_type"] == "OSError"
