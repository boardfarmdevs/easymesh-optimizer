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
