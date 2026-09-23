from dataclasses import replace
from unittest.mock import patch

from optimizer.candidates import CandidateMetricsError, CandidateMetricsUnavailable
from optimizer.rolling import RollingCandidateProvider
from .helpers import snapshot

import pytest


class Delegate:
    def __init__(self):
        self.client_selector = None
        self.last_raw = []
        self.last_rejected_candidate_keys = set()
        self.calls = []
        self.failure = None

    def __call__(self, clients, inventory, bsses, observed_at):
        selected = {client.sta_mac for client in clients if self.client_selector(client, observed_at)}
        self.calls.append(selected)
        self.last_raw = [{"selected": sorted(selected)}]
        if self.failure:
            raise self.failure
        return [replace(item, metric_observed_at=observed_at) for item in inventory if item.sta_mac in selected]


def fleet():
    sample = snapshot(0)
    clients = tuple(replace(sample.clients[0], sta_mac=f"02:00:00:00:{index:02x}:00",
                            band="5" if index < 5 else "2.4") for index in range(3, 7))
    candidates = tuple(replace(sample.candidates[0], sta_mac=client.sta_mac, band=client.band) for client in clients)
    return clients, candidates


def test_bounded_rounds_are_fair_and_reuse_exact_native_timestamps():
    clients, inventory = fleet()
    delegate = Delegate()
    provider = RollingCandidateProvider(delegate, maximum_clients=2)
    first = list(provider(clients, inventory, [], snapshot(0).observed_at))
    second = list(provider(clients, inventory, [], snapshot(1).observed_at))
    assert len(first) == 2 and len(second) == 4
    assert delegate.calls == [{client.sta_mac for client in clients[:2]}, {client.sta_mac for client in clients[2:]}]
    assert all(item.metric_observed_at == snapshot(0).observed_at for item in second if item.sta_mac in delegate.calls[0])
    assert len(provider.last_selected_sta_macs) == 4
    assert len(provider.last_requested_sta_macs) == 2


def test_roamed_clients_never_reuse_samples_from_the_old_source():
    clients, inventory = fleet()
    delegate = Delegate()
    provider = RollingCandidateProvider(delegate, maximum_clients=2)
    provider(clients, inventory, [], snapshot(0).observed_at)
    clients = (replace(clients[0], connected_bssid="02:00:00:aa:aa:02"), *clients[1:])
    delegate.failure = CandidateMetricsUnavailable("radio timeout")
    result = provider(clients, inventory, [], snapshot(1).observed_at)
    assert all(item.sta_mac != clients[0].sta_mac for item in result)
    assert provider.last_unavailable == "radio timeout"


def test_failed_cohort_does_not_discard_other_fresh_clients_or_starve_the_next_cohort():
    clients, inventory = fleet()
    delegate = Delegate()
    provider = RollingCandidateProvider(delegate, maximum_clients=2)
    first = provider(clients, inventory, [], snapshot(0).observed_at)
    delegate.failure = CandidateMetricsUnavailable("unavailable")
    second = provider(clients, inventory, [], snapshot(1).observed_at)
    assert second == first
    delegate.failure = None
    provider(clients, inventory, [], snapshot(2).observed_at)
    assert delegate.calls[0] == delegate.calls[2]


def test_world_inventory_and_age_invalidation_are_fail_closed():
    clients, inventory = fleet()
    identity = [1]
    delegate = Delegate()
    provider = RollingCandidateProvider(delegate, maximum_clients=2, maximum_age_seconds=5,
                                         identity=lambda: identity[0])
    provider(clients, inventory, [], snapshot(0).observed_at)
    delegate.failure = CandidateMetricsUnavailable("unavailable")
    assert provider(clients, inventory, [], snapshot(6).observed_at) == []
    delegate.failure = None
    provider(clients, inventory, [], snapshot(7).observed_at)
    identity[0] += 1
    delegate.failure = CandidateMetricsUnavailable("unavailable")
    assert provider(clients, inventory, [], snapshot(8).observed_at) == []


def test_invalid_native_identity_is_not_downgraded_to_an_unavailable_cohort():
    clients, inventory = fleet()
    delegate = Delegate()
    provider = RollingCandidateProvider(delegate)
    delegate.failure = CandidateMetricsError("wrong radio")
    with pytest.raises(CandidateMetricsError, match="wrong radio"):
        provider(clients, inventory, [], snapshot(0).observed_at)


def test_cache_age_is_checked_after_slow_native_collection_too():
    clients, inventory = fleet()
    provider = RollingCandidateProvider(Delegate(), maximum_age_seconds=5)
    with patch("optimizer.rolling.time.monotonic", side_effect=[100, 106]):
        assert provider(clients, inventory, [], snapshot(0).observed_at) == []
