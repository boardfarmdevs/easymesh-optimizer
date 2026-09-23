from copy import deepcopy
from dataclasses import replace
from importlib.util import find_spec
from types import SimpleNamespace

import pytest

from optimizer.candidates import CandidateMetricsUnavailable
from optimizer.load_observer import NativeLoadProvider
from optimizer.model import parse_time
from optimizer.observer import ControllerObserver
from optimizer.streaming import StreamingCandidateProvider
from .test_load_policy import loaded, SOURCE, STA
from .test_streaming import Delegate


BACKENDS = ["rdk"] + (["prpl"] if find_spec("optimizer.prplmesh") else [])


@pytest.fixture
def lab(monkeypatch):
    clock = [1]
    epoch = ["epoch"]
    monkeypatch.setattr("optimizer.load_observer.time.monotonic_ns", lambda: int(clock[0] * 1e9))
    monkeypatch.setattr("optimizer.load_observer.provenance", lambda *_args: epoch[0])
    provider = NativeLoadProvider()
    initial = loaded()
    client = initial.clients[0]
    target = "02:00:00:aa:aa:99"
    owner = "02:00:00:00:fe:20"
    moved = replace(initial, clients=(replace(client, connected_device_id=owner,
                                             connected_bssid=target),))
    raw = {"topology": {"nodes": [{"id": client.connected_device_id}, {"id": owner}], "edges": []},
           "bsses": {"bsses": [{"bssid": bssid, "device_id": device, "radio_id": bssid,
                                "channel": 36, "band": "5", "ssid": "private_ssid"}
                               for bssid, device in ((SOURCE, client.connected_device_id), (target, owner))]}}
    wall = parse_time(initial.observed_at).timestamp()

    def feed(seconds, snapshot=moved, transport="ieee1905-ethernet"):
        station = snapshot.clients[0]
        provider.ingest({"source": station.connected_device_id, "monotonic_ns": int(seconds * 1e9),
                         "received_at": wall + seconds, "transport": transport,
                         "loads": [{"bssid": station.connected_bssid, "utilization": 10, "station_count": 1}],
                         "traffic": [{"sta_mac": STA, "packets_sent": int(seconds * 100),
                                      "packets_received": int(seconds * 100)}]})

    def observe(snapshot, seconds):
        clock[0] = seconds
        provider.observe_owners(snapshot.clients, raw)

    def enrich(snapshot, seconds):
        clock[0] = seconds
        return provider.enrich(snapshot, raw)

    return SimpleNamespace(provider=provider, initial=initial, moved=moved, raw=raw,
                           clock=clock, epoch=epoch, feed=feed, observe=observe, enrich=enrich)


def observer_for(backend, lab, snapshot, candidates):
    if backend == "rdk":
        clients = [{"mac": client.sta_mac, "connected_bssid": client.connected_bssid,
                    "connected_ap_mac": client.connected_device_id,
                    "client_metrics": {"rcpi": client.rcpi, "last_updated": client.metric_observed_at}}
                   for client in snapshot.clients]
        payloads = {**lab.raw, "clients": {"clients": clients}, "devices": {"devices": []}}
        observer_type = ControllerObserver
        fetcher = lambda url: payloads[url.rsplit("/", 1)[-1]]
    else:
        from optimizer.prplmesh import PrplMeshObserver
        devices = []
        for bss in lab.raw["bsses"]["bsses"]:
            clients = [{"id": client.sta_mac, "signal_raw": client.rcpi,
                        "signal_updated_at": client.metric_observed_at}
                       for client in snapshot.clients if client.connected_bssid == bss["bssid"]]
            devices.append({"id": bss["device_id"], "radios": [
                {"id": bss["radio_id"], "band": "5 GHz", "channel": bss["channel"],
                 "bsses": [{"bssid": bss["bssid"], "ssid": bss["ssid"], "clients": clients}]}]})
        payloads = {"devices": devices}
        observer_type = PrplMeshObserver
        fetcher = lambda url: payloads
    calls = []

    def fetch(url):
        calls.append(url)
        return fetcher(url)

    observer = observer_type(fetcher=fetch, candidate_provider=candidates)
    observer.ownership_observer = lambda clients, raw: lab.provider.observe_owners(clients, raw)
    return observer, calls


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("same_agent", [False, True])
def test_new_target_baseline_survives_candidate_sweep(backend, same_agent, lab):
    if same_agent:
        owner = lab.initial.clients[0].connected_device_id
        lab.moved = replace(lab.moved, clients=(replace(lab.moved.clients[0], connected_device_id=owner),))
        lab.raw["bsses"]["bsses"][1]["device_id"] = owner
    lab.enrich(lab.initial, 1)
    lab.clock[0] = 2

    def candidates(*arguments):
        lab.feed(3, snapshot=lab.moved)
        lab.feed(8, snapshot=lab.moved)
        lab.clock[0] = 9
        return []

    observer, calls = observer_for(backend, lab, lab.moved, candidates)
    snapshot = observer.observe()
    result = lab.provider.enrich(snapshot, observer.last_raw)
    assert len(result.client_activity) == 1
    assert result.client_activity[0].interval_seconds == 5
    assert result.client_activity[0].packets_per_second == 200
    assert lab.provider.client_owners[STA][1] == 2000000000
    assert len(calls) == (4 if backend == "rdk" else 1)


@pytest.mark.parametrize("backend", BACKENDS)
def test_streaming_polls_retain_owner_baseline_without_extra_candidate_rounds(backend, lab):
    delegate = Delegate()
    streamer = StreamingCandidateProvider(delegate)
    lab.enrich(lab.initial, 1)
    lab.clock[0] = 2
    observer, calls = observer_for(backend, lab, lab.moved, streamer)
    try:
        snapshot = observer.observe()
        assert delegate.published.wait(1)
        assert not streamer._future.done()
        assert lab.provider.client_owners[STA][1] == 2000000000
        lab.feed(3)
        lab.clock[0] = 4
        assert lab.provider.enrich(snapshot, observer.last_raw).client_activity == ()
        lab.feed(8)
        lab.clock[0] = 9
        snapshot = observer.observe()
        activity = lab.provider.enrich(snapshot, observer.last_raw).client_activity
        assert len(activity) == 1
        assert activity[0].interval_seconds == 5
        assert lab.provider.client_owners[STA][1] == 2000000000
        assert not streamer._future.done()
        assert len(delegate.calls) == 1
        assert len(calls) == (8 if backend == "rdk" else 2)
    finally:
        delegate.release.set()
        streamer.close()


def test_first_observation_cannot_use_pre_owner_counters(lab):
    lab.enrich(replace(lab.initial, clients=()), 1)
    lab.feed(1.2)
    lab.feed(1.8)
    assert lab.enrich(lab.moved, 2).client_activity == ()
    lab.feed(3)
    assert lab.enrich(lab.moved, 4).client_activity == ()
    lab.feed(8)
    assert len(lab.enrich(lab.moved, 9).client_activity) == 1


@pytest.mark.parametrize("baseline", [1.5, 2])
def test_delayed_or_boundary_report_cannot_start_post_owner_interval(lab, baseline):
    lab.observe(lab.initial, 1)
    lab.observe(lab.moved, 2)
    lab.feed(baseline)
    lab.feed(3)
    assert lab.enrich(lab.moved, 4).client_activity == ()
    lab.feed(8)
    assert len(lab.enrich(lab.moved, 9).client_activity) == 1


def test_first_source_report_after_owner_observation_is_retained(lab):
    assert lab.moved.clients[0].connected_device_id != lab.initial.clients[0].connected_device_id
    lab.observe(lab.moved, 2)
    lab.feed(3)
    assert lab.enrich(lab.moved, 4).client_activity == ()
    lab.feed(8)
    assert len(lab.enrich(lab.moved, 9).client_activity) == 1
    assert lab.provider.client_owners[STA][1] == 2000000000


@pytest.mark.parametrize("change", ["provider", "channel", "radio", "disappearance", "return"])
def test_ownership_and_radio_epochs_require_two_new_counters(lab, change):
    lab.observe(lab.moved, 1)
    lab.feed(2)
    lab.feed(3)
    assert lab.enrich(lab.moved, 3).client_activity
    if change == "provider":
        lab.epoch[0] = "replacement"
    elif change in ("channel", "radio"):
        lab.raw["bsses"]["bsses"][1]["channel" if change == "channel" else "radio_id"] = (
            44 if change == "channel" else "02:00:00:aa:aa:98")
    elif change == "disappearance":
        lab.observe(replace(lab.moved, clients=()), 3.5)
    else:
        lab.observe(lab.initial, 3.5)
    lab.observe(lab.moved, 4)
    lab.feed(3.9)
    lab.feed(5)
    assert lab.enrich(lab.moved, 6).client_activity == ()
    lab.feed(10)
    assert lab.enrich(lab.moved, 11).client_activity[0].interval_seconds == 5


def test_transport_change_cannot_bridge_counter_intervals(lab):
    lab.observe(lab.moved, 1)
    lab.feed(2)
    lab.feed(3, transport="prpl-local-broker")
    assert lab.enrich(lab.moved, 4).client_activity == ()
    lab.feed(8, transport="prpl-local-broker")
    assert len(lab.enrich(lab.moved, 9).client_activity) == 1


def test_filtered_enrichment_does_not_forget_unfiltered_observed_owners(lab):
    lab.observe(lab.moved, 1)
    lab.feed(2)
    lab.enrich(replace(lab.moved, clients=()), 3)
    lab.feed(7)
    assert len(lab.enrich(lab.moved, 8).client_activity) == 1


@pytest.mark.parametrize("backend", BACKENDS)
def test_failed_candidate_sweep_still_observes_real_owner(backend, lab):
    lab.enrich(lab.initial, 1)
    lab.clock[0] = 2

    def fail(*arguments):
        lab.feed(3)
        raise CandidateMetricsUnavailable("candidate offline")

    observer, _calls = observer_for(backend, lab, lab.moved, fail)
    with pytest.raises(CandidateMetricsUnavailable, match="candidate offline"):
        observer.observe()
    lab.feed(8)
    assert len(lab.enrich(lab.moved, 9).client_activity) == 1


@pytest.mark.parametrize("backend", BACKENDS)
def test_ambiguous_ownership_never_reaches_hook_or_candidates(backend, lab, monkeypatch):
    clients = lab.moved.clients * 2
    ambiguous = SimpleNamespace(clients=clients)
    called = []
    observer, _calls = observer_for(backend, lab, ambiguous, lambda *arguments: called.append("candidate") or [])
    observer.ownership_observer = lambda *arguments: called.append("owner")
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    with pytest.raises((ValueError, CandidateMetricsUnavailable), match="duplicate"):
        observer.observe()
    assert called == []


def test_provider_rejects_ambiguous_batch_without_partial_owner_update(lab):
    lab.observe(lab.initial, 1)
    previous = deepcopy(lab.provider.client_owners)
    with pytest.raises(ValueError, match="duplicate"):
        lab.provider.observe_owners(lab.moved.clients * 2, lab.raw)
    assert lab.provider.client_owners == previous


def test_missing_or_wrong_inventory_owner_cannot_publish_activity(lab):
    lab.observe(lab.moved, 1)
    lab.feed(2)
    lab.feed(3)
    lab.raw["bsses"]["bsses"][1]["device_id"] = lab.initial.clients[0].connected_device_id
    lab.observe(lab.moved, 4)
    assert lab.enrich(lab.moved, 4).client_activity == ()


@pytest.mark.parametrize("backend", BACKENDS)
def test_early_hook_keeps_existing_metric_publisher_and_precedes_fallback(backend, lab):
    calls = []
    snapshot = replace(lab.moved, clients=(replace(lab.moved.clients[0], rcpi=None, metric_observed_at=None),))
    observer, _requests = observer_for(backend, lab, snapshot, lambda *arguments: calls.append("candidates") or [])
    if not hasattr(observer, "current_link_fallback"):
        pytest.skip("this mirror's RDK observer has no fallback publisher")
    observer.ownership_observer = lambda *arguments: calls.append("owner")
    observer.current_link_fallback = lambda client: calls.append("fallback") or client
    observer.current_link_progress = lambda client: calls.append("publisher")
    observer.observe()
    assert calls == ["owner", "fallback", "publisher", "candidates"]


def test_enrichment_keeps_real_interval_start_for_post_verification_gate(lab):
    lab.observe(lab.moved, 2)
    lab.feed(3)
    lab.feed(8)
    activity = lab.enrich(lab.moved, 9).client_activity[0]
    interval_start = parse_time(activity.observed_at).timestamp() - activity.interval_seconds
    original_start = parse_time(lab.initial.observed_at).timestamp() + 3
    verification_finished = original_start + 1
    assert interval_start == original_start
    assert interval_start < verification_finished


@pytest.mark.parametrize("backend", BACKENDS)
def test_failed_owner_hook_stops_candidate_work(backend, lab):
    candidates = []

    def fail(*arguments):
        raise ValueError("owner observation unavailable")

    observer, _calls = observer_for(backend, lab, lab.moved,
                                    lambda *arguments: candidates.append(arguments) or [])
    observer.ownership_observer = fail
    with pytest.raises(ValueError, match="owner observation unavailable"):
        observer.observe()
    assert candidates == []


def test_early_reset_evidence_survives_late_enrichment(lab):
    lab.observe(lab.initial, 1)
    lab.observe(lab.moved, 2)
    lab.feed(3)
    lab.enrich(lab.moved, 4)
    collection = lab.raw["load_collection"]
    assert collection["sampled_monotonic_ns"] == 2000000000
    assert collection["enriched_monotonic_ns"] == 4000000000
    assert len(collection["owner_resets"]) == 1
    assert collection["owner_resets"][0]["floor_monotonic_ns"] == 2000000000
    assert collection["owner_resets"][0]["discarded_report_monotonic_ns"] is None
