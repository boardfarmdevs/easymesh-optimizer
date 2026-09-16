from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import time

from .band_scan import NativeBandScanner, SOURCE
from .candidates import CandidateMetricsUnavailable
from .model import parse_time
from .policy import Evaluation, ThresholdPolicy


class BandThresholdPolicy(ThresholdPolicy):
    def _evaluate_client(self, snapshot, client, old, now, health_reason):
        decision, state = super()._evaluate_client(snapshot, client, old, now, health_reason)
        if decision.action == "steer":
            target = next(item for item in snapshot.candidates_for(client.sta_mac) if item.bssid == decision.target_bssid)
            if (client.measurement_source != SOURCE or target.measurement_source != SOURCE):
                return replace(decision, action="none", reason="band_measurement_direction_mismatch"), replace(state, phase="stable")
            if state.condition_since and min(parse_time(client.metric_observed_at), parse_time(target.metric_observed_at)) <= parse_time(state.condition_since):
                return replace(decision, action="none", reason="band_waiting_for_new_scan"), replace(state, phase="holding", pending_since=None)
        return decision, state


def received_scan_enabled(profile):
    settings = (profile or {}).get("profile", {})
    return len(settings.get("allowed_bands", [])) > 1 or settings.get("measurement_mode") == "received_same_band"


def same_band_received(profile):
    return (profile or {}).get("profile", {}).get("measurement_mode") == "received_same_band"


def band_policy(config, *, same_band=False):
    if same_band:
        return BandThresholdPolicy(replace(config, band_upgrade_enabled=False, load_aware_enabled=False,
                                           condition_hold_seconds=1, minimum_dwell_seconds=3,
                                           reject_stale_metrics_after_seconds=2))
    return BandThresholdPolicy(replace(config, band_upgrade_enabled=True, load_aware_enabled=False,
                                   current_rcpi_below=100, minimum_target_gain_rcpi=4,
                                   minimum_band_upgrade_target_rcpi=120, maximum_band_upgrade_loss_rcpi=16,
                                   condition_hold_seconds=1, minimum_dwell_seconds=3,
                                   reject_stale_metrics_after_seconds=2))


def evaluate_band_clients(policy, snapshot, prior, stations, profiles=None):
    if not stations:
        return policy.evaluate(snapshot, prior)
    received_only = {station for station in stations if same_band_received((profiles or {}).get(station))}
    if received_only:
        ordinary = evaluate_band_clients(policy, replace(snapshot, clients=tuple(
            client for client in snapshot.clients if client.sta_mac not in received_only)),
            prior, stations - received_only, profiles)
        profiled = band_policy(policy.config, same_band=True).evaluate(replace(snapshot, clients=tuple(
            client for client in snapshot.clients if client.sta_mac in received_only)), ordinary.state)
        digest = hashlib.sha256((ordinary.policy_hash + profiled.policy_hash +
                                 json.dumps(sorted(received_only))).encode()).hexdigest()
        return Evaluation(digest, tuple(sorted(ordinary.decisions + profiled.decisions,
                                               key=lambda item: item.sta_mac)), profiled.state)
    ordinary = policy.evaluate(replace(snapshot, clients=tuple(client for client in snapshot.clients
                                                              if client.sta_mac not in stations)), prior)
    profiled = band_policy(policy.config).evaluate(replace(snapshot, clients=tuple(client for client in snapshot.clients
                                                                                if client.sta_mac in stations)), ordinary.state)
    digest = hashlib.sha256((ordinary.policy_hash + profiled.policy_hash + json.dumps(sorted(stations))).encode()).hexdigest()
    return Evaluation(digest, tuple(sorted(ordinary.decisions + profiled.decisions, key=lambda item: item.sta_mac)), profiled.state)


def scan_eligibility(sample, profile, ssid):
    if sample["ssid"] != ssid:
        return "different_ssid"
    if sample["band"] not in profile["profile"]["allowed_bands"] or sample["frequency_mhz"] not in profile["frequencies_mhz"]:
        return "unsupported_or_disallowed_band"
    configured = set(profile["settings"]["key_mgmt"].split())
    capable = set(profile["capabilities"]["key_management"])
    advertised = set(sample["key_management"])
    if sample["band"] == "6":
        if not ("SAE" in configured & capable & advertised and sample["pmf_required"]):
            return "six_ghz_requires_sae_and_pmf"
    elif not (("WPA-PSK" in configured & capable and "PSK" in advertised)
              or "SAE" in configured & capable & advertised):
        return "incompatible_security"
    if sample["pmf_required"] and profile["settings"]["ieee80211w"] not in {"1", "2"}:
        return "pmf_not_enabled"
    return None


class BandSteeringMeasurements:
    def __init__(self, *, scanner=None, updated=None, telemetry=None, clock=None, executor=None):
        self.scanner = scanner or NativeBandScanner()
        self.updated = updated or (lambda: None)
        self.telemetry = telemetry or (lambda _value: None)
        self.clock = clock or time.monotonic
        self.executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="band-scan")
        self.pending = {}
        self.cache = {}
        self.attempted = {}
        self.selected = set()
        self.requested = set()
        self.status = {}
        self.jobs = {}

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)

    @staticmethod
    def _identity(client, profile, epoch):
        return (epoch, client.sta_mac, client.connected_bssid, client.band, client.ssid,
                json.dumps(profile, sort_keys=True))

    def enrich(self, snapshot, profiles, epoch):
        now = parse_time(snapshot.observed_at)
        self.selected = set()
        self.requested = set()
        self.jobs = {}
        self.status = {station: value for station, value in self.status.items() if station in profiles}
        self.cache = {station: value for station, value in self.cache.items() if station in profiles}
        clients, candidates = [], []
        for client in snapshot.clients:
            profile = profiles.get(client.sta_mac)
            inventory = snapshot.candidates_for(client.sta_mac)
            if profile is None:
                clients.append(client)
                candidates.extend(inventory)
                continue
            station = client.sta_mac
            identity = self._identity(client, profile, epoch)
            pending = self.pending.get(station)
            if pending is not None and pending[1].done():
                source_identity, future = self.pending.pop(station)
                try:
                    result = future.result()
                    if source_identity == identity:
                        self.cache[station] = (identity, result)
                        self.telemetry({"sta_mac": station, "phase": "received", "result": result})
                    else:
                        self.telemetry({"sta_mac": station, "phase": "discarded", "reason": "association_or_world_changed"})
                except (CandidateMetricsUnavailable, ValueError, OSError) as error:
                    self.status[station] = {"source": SOURCE, "available": False, "error": str(error)}
                    self.telemetry({"sta_mac": station, "phase": "unavailable", "error": str(error)})
            self.jobs[station] = (identity, client, profile)
            if station in self.pending:
                self.requested.add(station)
            cached_identity, result = self.cache.get(station, (None, None))
            valid = (cached_identity == identity and result is not None
                     and client.connected_bssid in result["samples"]
                     and all(0 <= (now - parse_time(sample["observed_at"])).total_seconds() <= 2
                             for sample in result["samples"].values()))
            if not valid:
                clients.append(replace(client, rcpi=None, metric_observed_at=None, measurement_source=SOURCE))
                candidates.extend(replace(item, rcpi=None, metric_observed_at=None, measurement_source=SOURCE,
                                          eligible=item.band in profile["profile"]["allowed_bands"]) for item in inventory)
                self.status.setdefault(station, {"source": SOURCE})
                self.status[station]["available"] = False
                continue
            current = result["samples"][client.connected_bssid]
            clients.append(replace(client, rcpi=current["rcpi"], metric_observed_at=current["observed_at"], measurement_source=SOURCE))
            rejected = {}
            for item in inventory:
                sample = result["samples"].get(item.bssid)
                reason = (scan_eligibility(sample, profile, client.ssid) if sample else "not_received_in_fresh_scan")
                if sample and sample["band"] != item.band:
                    reason = "native_inventory_band_mismatch"
                if reason:
                    rejected[item.bssid] = reason
                candidates.append(replace(item, rcpi=sample["rcpi"] if sample and reason is None else None,
                                          metric_observed_at=sample["observed_at"] if sample else None,
                                          measurement_source=SOURCE, eligible=reason is None))
            self.selected.add(station)
            self.status[station] = {"source": SOURCE, "available": True, "scan_id": result["scan_id"],
                                    "elapsed_ms": result["elapsed_ms"], "observed_at": current["observed_at"],
                                    "serving_bssid": client.connected_bssid, "rejected": rejected,
                                    "allowed_bands": profile["profile"]["allowed_bands"],
                                    "neighbors": [{"bssid": sample["bssid"],
                                        "frequency_mhz": sample["frequency_mhz"], "rcpi": sample["rcpi"],
                                        "observed_at": sample["observed_at"],
                                        "advertised_bss_load": sample["advertised_bss_load"]}
                                        for sample in result["samples"].values()]}
            self.status[station]["measurement_mode"] = profile["profile"].get("measurement_mode", "received_multiband")
        return replace(snapshot, clients=tuple(clients), candidates=tuple(candidates))

    def in_flight(self, station):
        return station in self.pending and not self.pending[station][1].done()

    def schedule(self, blocked=()):
        for station, (identity, client, profile) in self.jobs.items():
            if station in blocked or station in self.pending or self.clock() - self.attempted.get(station, float("-inf")) < 0.5:
                continue
            self.attempted[station] = self.clock()
            future = self.executor.submit(self.scanner.collect, profile["container"], sta_mac=station,
                                          bssid=client.connected_bssid, ssid=client.ssid,
                                          frequencies=profile["frequencies_mhz"], capabilities=profile["capabilities"])
            self.pending[station] = (identity, future)
            self.requested.add(station)
            future.add_done_callback(lambda _future: self.updated())


def band_fleet_status(fleet, snapshot, policy, stations, measurements, profiles=None):
    if not stations:
        return fleet
    evaluation = evaluate_band_clients(policy, snapshot, None, stations, profiles)
    settled = {"candidate_gain_too_small", "current_link_acceptable", "no_safe_band_upgrade"}
    waiting = [item.sta_mac for item in evaluation.decisions if item.reason not in settled]
    available = all(measurements.status.get(station, {}).get("available") for station in stations)
    received_only = {station for station in stations if same_band_received((profiles or {}).get(station))}
    policy_name = ("received-scan-same-band-v1" if received_only == stations else
                   "received-scan-mixed-v1" if received_only else "received-scan-band-preference-v1")
    return {**fleet, "policy": policy_name, "band_measurements_complete": available,
            "clients_outside_policy_margin": len(waiting), "band_unsettled_clients": waiting,
            "converged": fleet["measurement_complete"] and available and not waiting}
