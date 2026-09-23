from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from queue import Empty, SimpleQueue
import threading
import time

from .candidates import CandidateMetricsUnavailable, CandidateSnapshotSuperseded
from .model import parse_time
from .rolling import RollingCandidateProvider


class StreamingCandidateProvider:
    """Single-flight native collection with nonblocking, identity-bound publication."""

    def __init__(self, provider, *, maximum_clients=8, maximum_age_seconds=30,
                 identity=None, client_identity=None, updated=None, telemetry=None):
        if maximum_clients < 1 or maximum_age_seconds <= 0:
            raise ValueError("streaming collection requires positive bounds")
        self.provider = provider
        self.selector = provider.client_selector
        self.maximum_clients = maximum_clients
        self.maximum_age_seconds = maximum_age_seconds
        self.identity = identity
        self.client_identity = client_identity
        self.updated = updated or (lambda: None)
        self.telemetry = telemetry or (lambda _value: None)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="native-candidate-stream")
        self._messages = SimpleQueue()
        self._deferred = []
        self._progress_lock = threading.Lock()
        self._progress_update = None
        self._report_progress = getattr(provider, "progress", None)
        self._future = None
        self._cancel = threading.Event()
        self._closed = False
        self._identity = None
        self._owners = {}
        self._versions = {}
        self._cache = {}
        self._rejections = {}
        self._queried = {}
        self._round = 0
        self._next_attempt = 0.0
        self._active_selected = set()
        self.last_raw = []
        self.last_selection = {}
        self.last_selected_sta_macs = set()
        self.last_requested_sta_macs = set()
        self.last_rejected_candidate_keys = set()
        self.last_unavailable = None

    def close(self):
        self._closed = True
        self._cancel.set()
        self._executor.shutdown(wait=True)

    def _collect(self, clients, inventory, bsses, observed_at, selected, identity, versions, cancel):
        def progress(payload):
            with self._progress_lock:
                self._progress_update = (identity, dict(payload))
            self.updated()

        def publish(measured, rejected, transaction):
            self._messages.put((identity, versions, measured, rejected, transaction, time.monotonic()))
            self.updated()

        self.provider.client_selector = lambda client, _observed_at: client.sta_mac in selected
        self.provider.generation_guard = lambda: not cancel.is_set()
        self.provider.result_ready = publish
        self.provider.progress = progress if self._report_progress is not None else None
        try:
            self.provider(clients, inventory, bsses, observed_at)
            return None
        except CandidateSnapshotSuperseded:
            return "collection_superseded"
        except CandidateMetricsUnavailable as error:
            return str(error)
        finally:
            self.updated()

    def __call__(self, clients, inventory, bsses, observed_at):
        identity = (self.identity() if self.identity else None, tuple(sorted(
            tuple(str(item.get(key, "")) for key in ("bssid", "device_id", "radio_id", "band", "channel", "ssid"))
            for item in bsses)))
        if identity != self._identity:
            self._cancel.set()
            self._cache.clear()
            self._rejections.clear()
            self._deferred.clear()
            self._queried.clear()
            self._identity = identity
        owners = {client.sta_mac: (RollingCandidateProvider._owner(client),
                                  self.client_identity(client.sta_mac) if self.client_identity else None)
                  for client in clients}
        for station in self._owners.keys() | owners.keys():
            if self._owners.get(station) != owners.get(station):
                self._versions[station] = self._versions.get(station, 0) + 1
        self._owners = owners
        with self._progress_lock:
            progress = self._progress_update
            self._progress_update = None
        if progress is not None and progress[0] == identity and self._report_progress is not None:
            self._report_progress(progress[1])
        available = {(item.sta_mac, item.bssid) for item in inventory if item.eligible}
        now = parse_time(observed_at)

        def valid(key, version, timestamp, *, pending=False):
            return (key in available and key[0] in owners and self._versions.get(key[0]) == version
                    and timestamp is not None
                    and (-self.maximum_age_seconds if pending else 0)
                    <= (now - parse_time(timestamp)).total_seconds() <= self.maximum_age_seconds)

        self.last_raw = []
        messages, self._deferred = self._deferred, []
        while True:
            try:
                messages.append(self._messages.get_nowait())
            except Empty:
                break
        for message in messages:
            source_identity, versions, measured, rejected, transaction, ready_at = message
            finished = parse_time(transaction["finished_at"])
            if source_identity == identity and 0 < (finished - now).total_seconds() <= self.maximum_age_seconds:
                self._deferred.append(message)
                continue
            accepted = 0
            if source_identity == identity:
                for item in measured:
                    key = (item.sta_mac, item.bssid)
                    if (valid(key, versions.get(item.sta_mac), item.metric_observed_at)
                            and parse_time(item.metric_observed_at) <= finished):
                        self._cache[key] = (versions[item.sta_mac], item)
                        self._rejections.pop(key, None)
                        accepted += 1
                for key in rejected:
                    if valid(key, versions.get(key[0]), transaction["finished_at"]):
                        self._rejections[key] = (versions[key[0]], transaction["finished_at"])
                        self._cache.pop(key, None)
            self.last_raw.append(transaction)
            self.telemetry({"phase": "published", "accepted_comparisons": accepted,
                            "discarded_comparisons": len(measured) - accepted,
                            "publication_wait_ms": round((time.monotonic() - ready_at) * 1000, 3),
                            "transaction": transaction})
        if self._future is not None and self._future.done():
            self.last_unavailable = self._future.result()
            self.telemetry({"phase": "completed", "round": self._round,
                            "unavailable": self.last_unavailable,
                            "transactions": list(self.provider.last_raw)})
            self._future = None
            self._next_attempt = time.monotonic() + (0.25 if self.last_unavailable else 0)
        self._cache = {key: (version, item) for key, (version, item) in self._cache.items()
                       if valid(key, version, item.metric_observed_at)}
        self._rejections = {key: (version, timestamp) for key, (version, timestamp) in self._rejections.items()
                            if valid(key, version, timestamp)}
        eligible = [client for client in clients if self.selector is None or self.selector(client, observed_at)]
        eligible.sort(key=lambda client: (self._queried.get(client.sta_mac, -1), client.sta_mac))
        if not self._closed and self._future is None and eligible and time.monotonic() >= self._next_attempt:
            cohort = eligible[:self.maximum_clients]
            selected = {client.sta_mac for client in cohort}
            self._round += 1
            self._queried = {key: value for key, value in self._queried.items()
                             if key in owners}
            for station in selected:
                self._queried[station] = self._round
            self._active_selected = selected
            self._cancel = threading.Event()
            self._future = self._executor.submit(self._collect, clients, inventory, bsses, observed_at,
                                                  selected, identity, dict(self._versions), self._cancel)
        fresh = {key: item for key, (version, item) in self._cache.items()
                 if valid(key, version, item.metric_observed_at)}
        self.last_rejected_candidate_keys = {key for key, (version, timestamp) in self._rejections.items()
                                            if valid(key, version, timestamp)}
        self.last_requested_sta_macs = set(self._active_selected) if self._future is not None else set()
        self.last_selected_sta_macs = {key[0] for key in fresh}
        self.last_selection = {"eligible_clients": len(eligible), "selected_clients": len(self.last_requested_sta_macs),
                               "streaming": True, "round": self._round,
                               "collection_in_flight": self._future is not None,
                               "cached_native_comparisons": len(fresh),
                               "pending_snapshot_comparisons": sum(
                                   valid((item.sta_mac, item.bssid), versions.get(item.sta_mac),
                                         item.metric_observed_at, pending=True)
                                   for _identity, versions, measured, *_remaining in self._deferred for item in measured),
                               "maximum_cache_age_seconds": self.maximum_age_seconds,
                               "unavailable_cohort": sorted(self._active_selected) if self.last_unavailable else []}
        return list(fresh.values())
