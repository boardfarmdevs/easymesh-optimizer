from dataclasses import replace
import threading
import time

import pytest

from optimizer.candidates import CandidateMetricsError, CandidateMetricsUnavailable
from optimizer.streaming import StreamingCandidateProvider
from .helpers import snapshot


class Delegate:
    client_selector = None
    last_raw = []

    def __init__(self):
        self.published = threading.Event()
        self.release = threading.Event()
        self.failure = None
        self.calls = []

    def __call__(self, clients, inventory, bsses, observed_at):
        selected = {client.sta_mac for client in clients if self.client_selector(client, observed_at)}
        self.calls.append(selected)
        measured = tuple(item for item in inventory if item.sta_mac in selected)
        self.result_ready(measured, frozenset(), {"finished_at": observed_at})
        self.published.set()
        assert self.release.wait(2)
        if self.failure:
            raise self.failure
        return measured


@pytest.fixture
def streaming(request):
    delegate = Delegate()
    provider = StreamingCandidateProvider(delegate)
    def cleanup():
        delegate.release.set()
        provider.close()
    request.addfinalizer(cleanup)
    return provider, delegate


def collect(provider, sample=None):
    sample = sample or snapshot(0)
    return provider(sample.clients, sample.candidates, [], sample.observed_at)


def test_validated_result_is_actionable_before_slow_round_finishes(streaming):
    provider, delegate = streaming
    assert collect(provider) == []
    assert delegate.published.wait(1)
    assert collect(provider) == list(snapshot(0).candidates)
    assert not provider._future.done()
    assert len(delegate.calls) == 1
    assert provider.last_selection["collection_in_flight"]


def test_cache_expires_while_collection_is_blocked(streaming):
    provider, delegate = streaming
    collect(provider)
    assert delegate.published.wait(1)
    assert collect(provider)
    assert collect(provider, snapshot(31)) == []


def test_publication_during_snapshot_processing_is_retained_until_next_snapshot(streaming):
    provider, delegate = streaming
    collect(provider)
    assert delegate.published.wait(1)
    collect(provider)
    newer = snapshot(1)
    provider._messages.put((provider._identity, dict(provider._versions), newer.candidates, set(),
                            {"finished_at": newer.observed_at}, time.monotonic()))
    assert collect(provider) == list(snapshot(0).candidates)
    assert provider.last_selection["pending_snapshot_comparisons"] == len(newer.candidates)
    assert collect(provider, newer) == list(newer.candidates)
    assert provider.last_selection["pending_snapshot_comparisons"] == 0


def test_deferred_publication_cannot_cross_an_owner_change(streaming):
    provider, delegate = streaming
    collect(provider)
    assert delegate.published.wait(1)
    newer = snapshot(1)
    provider._messages.put((provider._identity, dict(provider._versions), newer.candidates, set(),
                            {"finished_at": newer.observed_at}, time.monotonic()))
    collect(provider)
    moved = replace(newer, clients=(replace(newer.clients[0], connected_bssid="02:00:00:aa:aa:02"),))
    assert collect(provider, moved) == []
    assert collect(provider, newer) == []


def test_deferred_rejection_does_not_remove_valid_evidence_before_snapshot_time(streaming):
    provider, delegate = streaming
    collect(provider)
    assert delegate.published.wait(1)
    assert collect(provider)
    sample = snapshot(0)
    key = (sample.candidates[0].sta_mac, sample.candidates[0].bssid)
    provider._messages.put((provider._identity, dict(provider._versions), (), {key},
                            {"finished_at": snapshot(1).observed_at}, time.monotonic()))
    assert collect(provider) == list(sample.candidates)
    assert not provider.last_rejected_candidate_keys
    assert collect(provider, snapshot(1)) == [item for item in sample.candidates if (item.sta_mac, item.bssid) != key]
    assert provider.last_rejected_candidate_keys == {key}


def test_roam_and_return_cannot_resurrect_queued_measurements(streaming):
    provider, delegate = streaming
    collect(provider)
    assert delegate.published.wait(1)
    original = snapshot(0)
    moved = replace(original, clients=(replace(original.clients[0], connected_bssid="02:00:00:aa:aa:02"),))
    assert collect(provider, moved) == []
    assert collect(provider, original) == []


def test_world_change_cancels_old_collector_and_discards_publication(streaming):
    provider, delegate = streaming
    world = [1]
    provider.identity = lambda: world[0]
    collect(provider)
    assert delegate.published.wait(1)
    world[0] = 2
    assert collect(provider) == []
    assert not delegate.generation_guard()


def test_rf_change_discards_cached_and_pending_measurements_without_cancelling_other_clients(streaming):
    provider, delegate = streaming
    epochs = {}
    provider.client_identity = lambda station: epochs.get(station, 0)
    original = snapshot(0)
    station = original.clients[0].sta_mac
    other = "02:00:00:00:04:00"
    sample = replace(original, clients=(*original.clients, replace(original.clients[0], sta_mac=other)),
                     candidates=(*original.candidates, replace(original.candidates[0], sta_mac=other)))
    collect(provider, sample)
    assert delegate.published.wait(1)
    assert len(collect(provider, sample)) == len(sample.candidates)
    versions = dict(provider._versions)
    provider._messages.put((provider._identity, versions, sample.candidates, set(),
                            {"finished_at": sample.observed_at}, time.monotonic()))
    epochs[station] = 1
    assert {item.sta_mac for item in collect(provider, sample)} == {other}
    assert delegate.generation_guard()
    assert len(delegate.calls) == 1
    epochs[station] = 2
    provider._messages.put((provider._identity, versions, sample.candidates, set(),
                            {"finished_at": sample.observed_at}, time.monotonic()))
    assert {item.sta_mac for item in collect(provider, sample)} == {other}


def test_rf_change_invalidates_rejections_and_preserves_fair_queue_position(streaming):
    provider, delegate = streaming
    epoch = [0]
    provider.client_identity = lambda _station: epoch[0]
    sample = snapshot(0)
    collect(provider)
    assert delegate.published.wait(1)
    collect(provider)
    key = (sample.clients[0].sta_mac, sample.candidates[0].bssid)
    provider._rejections[key] = (provider._versions[key[0]], sample.observed_at)
    queried = dict(provider._queried)
    epoch[0] = 1
    assert collect(provider) == []
    assert not provider.last_rejected_candidate_keys
    assert provider._queried == queried


@pytest.mark.parametrize("failure", [CandidateMetricsError("wrong radio"), CandidateMetricsUnavailable("busy")])
def test_failure_classification_is_preserved(streaming, failure):
    provider, delegate = streaming
    delegate.failure = failure
    collect(provider)
    assert delegate.published.wait(1)
    delegate.release.set()
    try:
        provider._future.result(timeout=1)
    except CandidateMetricsError:
        pass
    if isinstance(failure, CandidateMetricsUnavailable):
        assert collect(provider)
        assert provider.last_unavailable == "busy"
    else:
        with pytest.raises(CandidateMetricsError, match="wrong radio"):
            collect(provider)


def test_no_extra_native_requests_while_policy_consumes_cached_results(streaming):
    provider, delegate = streaming
    collect(provider)
    assert delegate.published.wait(1)
    started = time.monotonic()
    for repeat in range(100):
        assert collect(provider)
    assert time.monotonic() - started < 0.5
    assert len(delegate.calls) == 1


def test_fair_cohorts_after_round_completion(streaming):
    provider, delegate = streaming
    provider.maximum_clients = 1
    sample = snapshot(0)
    other = "02:00:00:00:04:00"
    sample = replace(sample, clients=(*sample.clients, replace(sample.clients[0], sta_mac=other)),
                     candidates=(*sample.candidates, replace(sample.candidates[0], sta_mac=other)))
    collect(provider, sample)
    assert delegate.published.wait(1)
    delegate.release.set()
    provider._future.result(timeout=1)
    collect(provider, sample)
    provider._future.result(timeout=1)
    assert delegate.calls == [{sample.clients[0].sta_mac}, {other}]


def test_roaming_low_mac_cannot_starve_an_unmeasured_band(streaming):
    provider, delegate = streaming
    provider.maximum_clients = 1
    sample = snapshot(0)
    waiting = "02:00:00:00:0e:00"
    sample = replace(sample, clients=(*sample.clients, replace(sample.clients[0], sta_mac=waiting, band="6")),
                     candidates=(*sample.candidates, replace(sample.candidates[0], sta_mac=waiting, band="6")))
    collect(provider, sample)
    assert delegate.published.wait(1)
    delegate.release.set()
    provider._future.result(timeout=1)
    roamed = replace(sample, clients=(replace(sample.clients[0], connected_bssid="02:00:00:aa:aa:02"), sample.clients[1]))
    collect(provider, roamed)
    provider._future.result(timeout=1)
    assert delegate.calls == [{sample.clients[0].sta_mac}, {waiting}]


def test_large_cohort_publishes_incrementally_without_blocking_observations(streaming):
    provider, delegate = streaming
    provider.maximum_clients = 64
    original = snapshot(0)
    stations = [f"02:00:00:00:{index:02x}:00" for index in range(1, 51)]
    sample = replace(original,
                     clients=tuple(replace(original.clients[0], sta_mac=station) for station in stations),
                     candidates=tuple(replace(original.candidates[0], sta_mac=station) for station in stations))
    assert collect(provider, sample) == []
    assert delegate.published.wait(1)
    assert len(collect(provider, sample)) == 50
    assert delegate.calls == [set(stations)]
    assert not provider._future.done()


def test_sparse_bands_share_a_collection_round(streaming):
    provider, delegate = streaming
    original = snapshot(0)
    other = "02:00:00:00:0e:00"
    sample = replace(original,
                     clients=(*original.clients, replace(original.clients[0], sta_mac=other, band="6")),
                     candidates=(*original.candidates, replace(original.candidates[0], sta_mac=other, band="6")))
    collect(provider, sample)
    assert delegate.published.wait(1)
    assert delegate.calls == [{original.clients[0].sta_mac, other}]
    assert len(collect(provider, sample)) == len(sample.candidates)


def test_a_same_band_provider_takes_one_band_per_round(request):
    delegate = Delegate()
    delegate.same_band_cohort = True
    provider = StreamingCandidateProvider(delegate)
    request.addfinalizer(lambda: (delegate.release.set(), provider.close()))
    original = snapshot(0)
    other = "02:00:00:00:0e:00"
    sample = replace(original,
                     clients=(*original.clients, replace(original.clients[0], sta_mac=other, band="6")),
                     candidates=(*original.candidates, replace(original.candidates[0], sta_mac=other, band="6")))
    collect(provider, sample)
    assert delegate.published.wait(1)
    assert len(delegate.calls) == 1 and len(delegate.calls[0]) == 1


def test_progress_is_coalesced_off_the_native_request_thread():
    callback_threads = []
    payloads = []
    class ProgressDelegate(Delegate):
        def __call__(self, *arguments):
            for completed in range(100):
                self.progress({"completed_queries": completed})
            return super().__call__(*arguments)

    delegate = ProgressDelegate()
    def report(payload):
        callback_threads.append(threading.get_ident())
        payloads.append(payload)
    delegate.progress = report
    provider = StreamingCandidateProvider(delegate)
    try:
        collect(provider)
        assert delegate.published.wait(1)
        assert not payloads
        assert collect(provider)
        assert payloads == [{"completed_queries": 99}]
        assert callback_threads == [threading.get_ident()]
        collect(provider)
        assert len(payloads) == 1
        provider._progress_update = ("previous-world", {"completed_queries": 42})
        collect(provider)
        assert len(payloads) == 1
    finally:
        delegate.release.set()
        provider.close()


class RecordingDelegate(Delegate):
    def __init__(self):
        super().__init__()
        self.inventories = []
        self.release.set()

    def __call__(self, clients, inventory, bsses, observed_at):
        self.inventories.append({(item.sta_mac, item.bssid) for item in inventory})
        return super().__call__(clients, inventory, bsses, observed_at)


def _two_stations(sample):
    other = "02:00:00:00:04:00"
    return replace(sample, clients=(*sample.clients, replace(sample.clients[0], sta_mac=other)),
                   candidates=(*sample.candidates, replace(sample.candidates[0], sta_mac=other))), other


def test_incremental_round_asks_only_for_due_pairs():
    delegate = RecordingDelegate()
    provider = StreamingCandidateProvider(delegate, maximum_age_seconds=30, refresh_after_seconds=15)
    try:
        sample, other = _two_stations(snapshot(0))
        collect(provider, sample)
        provider._future.result(timeout=1)
        assert len(collect(provider, sample)) == 2
        # everything fresh: no round is started
        assert provider._future is None and len(delegate.calls) == 1
        # one station roams: the next round asks only for its pairs
        roamed = replace(sample, clients=(replace(sample.clients[0], connected_bssid="02:00:00:aa:aa:02"),
                                          sample.clients[1]))
        collect(provider, roamed)
        provider._future.result(timeout=1)
        assert delegate.inventories[-1] == {(sample.clients[0].sta_mac, sample.candidates[0].bssid)}
        assert delegate.calls[-1] == {sample.clients[0].sta_mac}
    finally:
        provider.close()


def test_incremental_round_refreshes_aging_pairs():
    delegate = RecordingDelegate()
    provider = StreamingCandidateProvider(delegate, maximum_age_seconds=30, refresh_after_seconds=15)
    try:
        sample, other = _two_stations(snapshot(0))
        collect(provider, sample)
        provider._future.result(timeout=1)
        collect(provider, sample)
        later = replace(sample, observed_at=snapshot(16).observed_at)
        collect(provider, later)
        provider._future.result(timeout=1)
        assert len(delegate.calls) == 2 and delegate.inventories[-1] == delegate.inventories[0]
    finally:
        provider.close()


def test_refresh_age_must_lie_within_the_maximum_age():
    with pytest.raises(ValueError):
        StreamingCandidateProvider(Delegate(), maximum_age_seconds=30, refresh_after_seconds=31)


def test_missing_pairs_go_before_a_refresh_wave():
    delegate = RecordingDelegate()
    provider = StreamingCandidateProvider(delegate, maximum_age_seconds=30, refresh_after_seconds=15)
    try:
        sample, other = _two_stations(snapshot(0))
        collect(provider, sample)
        provider._future.result(timeout=1)
        collect(provider, sample)
        # 16 s later both pairs are due for refresh, and one station has just moved
        later = replace(sample, observed_at=snapshot(16).observed_at,
                        clients=(replace(sample.clients[0], connected_bssid="02:00:00:aa:aa:02"),
                                 sample.clients[1]))
        collect(provider, later)
        provider._future.result(timeout=1)
        assert delegate.inventories[-1] == {(sample.clients[0].sta_mac, sample.candidates[0].bssid)}
        # with nothing missing any more, the refresh follows (this fake delegate
        # returns the 16 s old sample time, so the moved pair is due again too)
        collect(provider, later)
        provider._future.result(timeout=1)
        assert (other, sample.candidates[1].bssid) in delegate.inventories[-1]
    finally:
        provider.close()


def test_a_pair_that_is_never_answered_cannot_starve_refreshes():
    class Silent(RecordingDelegate):
        def __call__(self, clients, inventory, bsses, observed_at):
            self.inventories.append({(item.sta_mac, item.bssid) for item in inventory})
            selected = {client.sta_mac for client in clients if self.client_selector(client, observed_at)}
            self.calls.append(selected)
            measured = tuple(item for item in inventory
                             if item.sta_mac in selected and item.sta_mac != "02:00:00:00:04:00")
            self.result_ready(measured, frozenset(), {"finished_at": observed_at})
            return measured
    delegate = Silent()
    provider = StreamingCandidateProvider(delegate, maximum_age_seconds=30, refresh_after_seconds=15)
    try:
        sample, other = _two_stations(snapshot(0))
        collect(provider, sample)
        provider._future.result(timeout=1)
        later = replace(sample, observed_at=snapshot(16).observed_at)
        rounds = []
        for _ in range(3):
            collect(provider, later)
            provider._future.result(timeout=1)
            rounds.append(delegate.inventories[-1])
        refreshed = (sample.clients[0].sta_mac, sample.candidates[0].bssid)
        assert any(refreshed in inventory for inventory in rounds)
    finally:
        provider.close()
