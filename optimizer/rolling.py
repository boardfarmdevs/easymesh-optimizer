from __future__ import annotations

from datetime import timedelta
import time

from .candidates import CandidateMetricsUnavailable
from .model import parse_time


class RollingCandidateProvider:
    """Fair, bounded same-band rounds backed only by identity-valid native samples."""

    def __init__(self, provider, *, maximum_clients=8, maximum_age_seconds=30, identity=None):
        if maximum_clients < 1 or maximum_age_seconds <= 0:
            raise ValueError("rolling collection requires positive bounds")
        self.provider = provider
        self.selector = provider.client_selector
        self.provider.client_selector = lambda client, _observed_at: client.sta_mac in self._selected
        self.maximum_clients = maximum_clients
        self.maximum_age_seconds = maximum_age_seconds
        self.identity = identity
        self._identity = None
        self._selected = set()
        self._cache = {}
        self._rejections = {}
        self._queried = {}
        self._round = 0
        self.last_raw = []
        self.last_selection = {}
        self.last_selected_sta_macs = set()
        self.last_requested_sta_macs = set()
        self.last_rejected_candidate_keys = set()
        self.last_unavailable = None

    @staticmethod
    def _owner(client):
        return (client.connected_bssid, client.connected_device_id, client.band, client.ssid)

    def __call__(self, clients, inventory, bsses, observed_at):
        started = time.monotonic()
        identity = (self.identity() if self.identity else None, tuple(sorted(
            tuple(str(item.get(key, "")) for key in ("bssid", "device_id", "radio_id", "band", "channel", "ssid"))
            for item in bsses)))
        if identity != self._identity:
            self._cache.clear()
            self._rejections.clear()
            self._queried.clear()
            self._identity = identity
        owners = {client.sta_mac: self._owner(client) for client in clients}
        available = {(item.sta_mac, item.bssid) for item in inventory if item.eligible}
        now = parse_time(observed_at)

        def valid(key, owner, timestamp):
            return (key in available and owners.get(key[0]) == owner and timestamp is not None
                    and 0 <= (now - parse_time(timestamp)).total_seconds() <= self.maximum_age_seconds)

        self._cache = {key: (owner, item) for key, (owner, item) in self._cache.items()
                       if valid(key, owner, item.metric_observed_at)}
        self._rejections = {key: (owner, timestamp) for key, (owner, timestamp) in self._rejections.items()
                            if valid(key, owner, timestamp)}
        self._queried = {key: value for key, value in self._queried.items() if key[0] in owners and key[1] == owners[key[0]]}
        eligible = [client for client in clients if self.selector is None or self.selector(client, observed_at)]
        eligible.sort(key=lambda client: (self._queried.get((client.sta_mac, owners[client.sta_mac]), -1), client.sta_mac))
        cohort = [client for client in eligible if client.band == eligible[0].band][:self.maximum_clients] if eligible else []
        self._selected = {client.sta_mac for client in cohort}
        self._round += 1
        for station in self._selected:
            self._queried[(station, owners[station])] = self._round
        self._cache = {key: value for key, value in self._cache.items() if key[0] not in self._selected}
        self._rejections = {key: value for key, value in self._rejections.items() if key[0] not in self._selected}
        self.last_unavailable = None
        try:
            measured = list(self.provider(clients, inventory, bsses, observed_at)) if cohort else []
        except CandidateMetricsUnavailable as error:
            self.last_unavailable = str(error)
            measured = []
        self.last_raw = list(self.provider.last_raw) if cohort else []
        for item in measured:
            self._cache[(item.sta_mac, item.bssid)] = (owners[item.sta_mac], item)
        if cohort and self.last_unavailable is None:
            for key in self.provider.last_rejected_candidate_keys:
                self._rejections[key] = (owners[key[0]], observed_at)
        now += timedelta(seconds=time.monotonic() - started)
        self._cache = {key: (owner, item) for key, (owner, item) in self._cache.items()
                       if valid(key, owner, item.metric_observed_at)}
        self._rejections = {key: (owner, timestamp) for key, (owner, timestamp) in self._rejections.items()
                            if valid(key, owner, timestamp)}
        self.last_rejected_candidate_keys = set(self._rejections)
        self.last_requested_sta_macs = set(self._selected)
        self.last_selected_sta_macs = {key[0] for key in self._cache}
        self.last_selection = {"eligible_clients": len(eligible), "selected_clients": len(cohort),
                               "deferred_clients": len(eligible) - len(cohort), "priority_clients": 0,
                               "rolling": True, "round": self._round,
                               "cached_native_comparisons": len(self._cache),
                               "maximum_cache_age_seconds": self.maximum_age_seconds,
                               "unavailable_cohort": sorted(self._selected) if self.last_unavailable else []}
        return [item for _owner, item in self._cache.values()]
