from __future__ import annotations

from dataclasses import asdict, replace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from optimizer.actuator import NativeSteerActuator, SteerActuator
from optimizer.candidates import (
    CandidateMetricsError, CandidateMetricsUnavailable, CandidateSnapshotSuperseded,
    ControllerCandidateProvider,
)
from optimizer.config import load_policy
from optimizer.model import normalize_band, parse_time
from optimizer.observer import ControllerObserver
from optimizer.policy import ThresholdPolicy
from optimizer.streaming import StreamingCandidateProvider
from optimizer.state import ClientPolicyState, PolicyState
from optimizer.verifier import OutcomeVerifier
from wmdcfg.observers import mesh_health

from .events import EventStore
from .client_wifi import read_client_link
from .interactions import InteractionError


DEVICE_ROLES = {
    "agent-1": "gateway",
    "extender-1": "extender_1",
    "extender-2": "extender_2",
    "extender-3": "extender_3",
    "extender-4": "extender_4",
}


CANDIDATE_PRIORITY_WINDOW_SECONDS = 120


def _action_measurements_fresh(decision, snapshot, maximum_age, now):
    client = snapshot.client(decision.sta_mac)
    candidate = next((item for item in snapshot.candidates_for(decision.sta_mac)
                      if item.bssid == decision.target_bssid and item.eligible), None)
    return all(item is not None and item.metric_observed_at is not None
               and 0 <= (now - parse_time(item.metric_observed_at)).total_seconds() <= maximum_age
               for item in (client, candidate))


def _candidate_measurement_needed(policy, state, client, observed_at):
    if not policy.requires_candidate_measurement(client, observed_at):
        return False
    prior = state.for_sta(client.sta_mac)
    if prior.phase == "pending":
        return False
    until = prior.cooldown_until if prior.phase == "cooldown" else (
        prior.backoff_until if prior.phase == "backoff" else None
    )
    return until is None or parse_time(observed_at) >= parse_time(until)


def _priority_client(client, role, mac_by_role, ap_role_by_bssid, deadline, now):
    roles = (role,) if isinstance(role, str) else (role or ())
    return now < deadline and any(
        client.sta_mac == mac_by_role.get(changed)
        or ap_role_by_bssid.get(client.connected_bssid) == changed
        for changed in roles
    )


def _interactive_policy(config):
    return replace(config, current_rcpi_below=220, minimum_target_gain_rcpi=4,
                   condition_hold_seconds=0, minimum_dwell_seconds=0,
                   steer_timeout_seconds=40,
                   post_steer_cooldown_seconds=5, failure_backoff_seconds=5,
                   maximum_failure_backoff_seconds=30)


def _completed_action_state(state, decision, config, success, reason, now):
    prior = state.for_sta(decision.sta_mac)
    failures = 0 if success else prior.failure_count + 1
    delay = config.post_steer_cooldown_seconds if success else min(
        config.maximum_failure_backoff_seconds, config.failure_backoff_seconds * 2 ** min(failures - 1, 30)
    )
    until = (now + timedelta(seconds=delay)).isoformat()
    return state.replace(ClientPolicyState(
        sta_mac=decision.sta_mac, phase="cooldown" if success else "backoff",
        source_bssid=decision.target_bssid if success else decision.source_bssid,
        cooldown_until=until if success else None, backoff_until=None if success else until,
        failure_count=failures, last_failure_reason=None if success else reason,
        last_action_at=prior.last_action_at or now.isoformat(),
    ))


def _world_device_name(role: str | None) -> str | None:
    if role == "gateway":
        return "Agent-1"
    if role and role.startswith("extender_"):
        return "Extender-" + role.rsplit("_", 1)[1]
    return None


def _recommendation_state(prior: PolicyState, evaluation) -> PolicyState:
    state = evaluation.state
    for decision in evaluation.decisions:
        if decision.action != "steer":
            continue
        proposed = state.for_sta(decision.sta_mac)
        previous = prior.for_sta(decision.sta_mac)
        state = state.replace(replace(
            proposed,
            phase="recommended",
            pending_since=None,
            last_action_at=previous.last_action_at,
        ))
    return state


def _deferred_state(prior: PolicyState, evaluation) -> PolicyState:
    """Keep a satisfied condition eligible without pretending it was sent."""
    state = evaluation.state
    for decision in evaluation.decisions:
        if decision.action != "steer":
            continue
        proposed = state.for_sta(decision.sta_mac)
        previous = prior.for_sta(decision.sta_mac)
        state = state.replace(replace(
            proposed,
            phase="holding",
            pending_since=None,
            last_action_at=previous.last_action_at,
        ))
    return state


def _single_action_state(
    prior: PolicyState, evaluation, selected_sta: str
) -> PolicyState:
    """Keep all non-selected steer candidates eligible for the next cycle."""
    state = evaluation.state
    for decision in evaluation.decisions:
        if decision.action != "steer" or decision.sta_mac == selected_sta:
            continue
        proposed = state.for_sta(decision.sta_mac)
        previous = prior.for_sta(decision.sta_mac)
        state = state.replace(replace(
            proposed,
            phase="holding",
            pending_since=None,
            last_action_at=previous.last_action_at,
        ))
    return state


def _interrupted_measurement_state(state: PolicyState) -> PolicyState:
    """Restart unacted holds without discarding cooldown or action history."""
    return PolicyState(tuple(
        replace(item, phase="stable", source_bssid=None, target_bssid=None,
                condition_since=None)
        if item.phase in {"holding", "recommended"} else item
        for item in state.clients
    ))


def _ranked_action_batch(decisions, limit: int):
    """Choose a bounded fleet batch, moving weakest serving links first.

    Every decision in the batch comes from one controller-measured candidate
    snapshot. Actions remain sequential and individually verified; batching
    avoids repeating the expensive whole-fleet candidate query after every
    single BTM request.
    """
    if limit <= 0:
        return []
    actionable = [item for item in decisions if item.action == "steer"]
    actionable.sort(key=lambda item: (
        item.current_rcpi if item.current_rcpi is not None else 221,
        -(
            (item.target_rcpi if item.target_rcpi is not None else -1)
            - (item.current_rcpi if item.current_rcpi is not None else -1)
        ),
        item.sta_mac,
    ))
    return actionable[:limit]


def _fleet_status(snapshot, selected_sta_macs: set[str], max_age_seconds=60, minimum_gain_rcpi=1,
                  expected_sta_macs=None, native_sta_macs=None) -> dict[str, Any]:
    """Summarize measured best-AP convergence independently of policy phase."""
    better: list[dict[str, Any]] = []
    fresh_clients = set()
    now = parse_time(snapshot.observed_at)

    def fresh(timestamp):
        return timestamp is not None and 0 <= (now - parse_time(timestamp)).total_seconds() <= max_age_seconds

    for client in snapshot.clients:
        if client.rcpi is None or not fresh(client.metric_observed_at):
            continue
        eligible = [
            item for item in snapshot.candidates_for(client.sta_mac)
            if item.eligible and item.band == client.band
            and item.bssid != client.connected_bssid
        ]
        candidates = [item for item in eligible if item.rcpi is not None and fresh(item.metric_observed_at)]
        if not candidates:
            continue
        if len(candidates) == len(eligible):
            fresh_clients.add(client.sta_mac)
        best = max(candidates, key=lambda item: (int(item.rcpi), item.bssid))
        if int(best.rcpi) > int(client.rcpi):
            better.append({
                "sta_mac": client.sta_mac,
                "current_bssid": client.connected_bssid,
                "current_rcpi": client.rcpi,
                "target_bssid": best.bssid,
                "target_rcpi": best.rcpi,
                "gain_rcpi": int(best.rcpi) - int(client.rcpi),
            })
    checked = len(selected_sta_macs & fresh_clients)
    total = len(snapshot.clients)
    actionable = [item for item in better if item["gain_rcpi"] >= minimum_gain_rcpi]
    actual_sta_macs = ({client.sta_mac for client in snapshot.clients}
                       if native_sta_macs is None else set(native_sta_macs))
    expected_sta_macs = actual_sta_macs if expected_sta_macs is None else set(expected_sta_macs)
    roster_complete = actual_sta_macs == expected_sta_macs
    return {
        "roster_complete": roster_complete,
        "missing_clients": sorted(expected_sta_macs - actual_sta_macs),
        "unexpected_clients": sorted(actual_sta_macs - expected_sta_macs),
        "clients_evaluated": total,
        "clients_checked": checked,
        "candidate_measurements": sum(
            item.rcpi is not None and fresh(item.metric_observed_at) for item in snapshot.candidates
        ),
        "clients_with_stronger_ap": len(better),
        "clients_outside_policy_margin": len(actionable),
        "minimum_gain_rcpi": minimum_gain_rcpi,
        "absolute_best_converged": roster_complete and checked == total and total > 0 and not better,
        "stronger_candidates": sorted(
            better, key=lambda item: (-item["gain_rcpi"], item["sta_mac"])
        ),
        "measurement_complete": roster_complete and checked == total,
        "converged": roster_complete and checked == total and total > 0 and not actionable,
    }


def _client_optimizer_status(snapshot, evaluation, config, selected_sta_macs):
    clients = {client.sta_mac: client for client in snapshot.clients}
    now = parse_time(snapshot.observed_at)
    result = []
    for decision in evaluation.decisions:
        client = clients.get(decision.sta_mac)
        if client is None:
            continue
        state = evaluation.state.for_sta(client.sta_mac)
        remaining = 0.0
        if decision.reason == "minimum_dwell_not_met":
            remaining = max(0, config.minimum_dwell_seconds - client.association_uptime_seconds)
        elif decision.reason == "condition_hold_not_met":
            remaining = max(0, config.condition_hold_seconds - decision.hold_seconds)
        elif decision.reason == "post_steer_cooldown" and state.cooldown_until:
            remaining = max(0, (parse_time(state.cooldown_until) - now).total_seconds())
        elif decision.reason == "steer_failure_backoff" and state.backoff_until:
            remaining = max(0, (parse_time(state.backoff_until) - now).total_seconds())
        result.append({
            **decision.to_dict(),
            "phase": state.phase,
            "association_uptime_seconds": client.association_uptime_seconds,
            "metric_observed_at": client.metric_observed_at,
            "measurement_source": client.measurement_source,
            "candidate_query_selected": client.sta_mac in selected_sta_macs,
            "wait_remaining_seconds": round(remaining, 1),
        })
    return result


def _rssi(rcpi: int | None) -> int | None:
    return None if rcpi is None else int(round(rcpi / 2 - 110))


def _channel_for_frequency(band: str, frequency_mhz: int) -> int:
    """Translate the compiled live hwsim frequency to its 20 MHz channel."""
    if band == "2.4":
        return 14 if frequency_mhz == 2484 else (frequency_mhz - 2407) // 5
    if band == "5":
        return (frequency_mhz - 5000) // 5
    if band == "6":
        return (frequency_mhz - 5950) // 5
    raise ValueError(f"unsupported compiled radio band {band!r}")


def _simulated_bss_channels(plan: dict[str, Any]) -> dict[str, int]:
    """Map each live simulated BSSID to its compiled operating channel."""
    result: dict[str, int] = {}
    for value in plan["bindings"].values():
        if value["role_type"] != "fronthaul_ap":
            continue
        for band, radio in value.get("band_radios", {}).items():
            fallback_frequency = radio.get("frequency_mhz")
            for interface in radio.get("interfaces", []):
                mac = interface.get("mac")
                frequency = interface.get("frequency_mhz", fallback_frequency)
                if mac and frequency:
                    result[mac.lower()] = _channel_for_frequency(
                        band, int(frequency)
                    )
    if not result:
        raise ValueError("compiled room has no live simulated BSS channels")
    return result


class LiveConductor:
    """Join live controller truth, optimizer state, traffic and health to one run."""

    def __init__(
        self,
        store: EventStore,
        plan: dict[str, Any],
        manifest: dict[str, Any],
        *,
        mode: str,
        repo_root: Path,
        base_url: str = "http://127.0.0.1:8888",
        room_state: Callable[[], dict[str, Any]] | None = None,
        room_projection: Callable[[], dict[str, Any] | None] | None = None,
        interactive: bool = False,
        profiling: bool = False,
        maximum_actions: int | None = None,
        steering_transaction: Callable[
            [str, str, str, str, Callable[[], Any]], Any
        ] | None = None,
        backhaul_manager: Any = None,
    ) -> None:
        if mode not in {"stimulus", "recommend", "act"}:
            raise ValueError(f"unsupported demo mode {mode!r}")
        self.store = store
        self.plan = plan
        self.manifest = manifest
        self.mode = mode
        self.repo_root = repo_root
        self.base_url = base_url
        self.room_state = room_state
        self.room_projection = room_projection
        self.interactive = interactive
        self.profiling = profiling
        self.maximum_actions = maximum_actions
        self.steering_transaction = None if profiling else steering_transaction
        self.coordination = {"candidate_parallel_agents": 1, "negotiated": False}
        self.backhaul_manager = backhaul_manager
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.action_attempts = 0
        self.action_successes = 0
        self.verification_successes = 0
        self._error_lock = threading.Lock()
        self._controller_lock = threading.Lock()
        self._candidate_active = threading.Event()
        self._candidate_updated = threading.Event()
        self._streaming_provider = None
        self._link_sample_lock = threading.Lock()
        self._client_sample_locks: dict[str, Any] = {}
        self._probe_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="room-link-probe")
        self._verification_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="room-verification")
        self._network_lock = threading.Lock()
        self._network_clients = ()
        self._network_observed_at = 0.0
        self._network_metrics = {}
        self._link_samples: dict[tuple[Any, ...], tuple[float, dict[str, Any] | None]] = {}
        self._role_by_mac = {
            value["radio_permanent_mac"].lower(): role
            for role, value in plan["bindings"].items()
            if value["role_type"] == "station"
        }
        self._container_by_mac = {
            value["radio_permanent_mac"].lower(): value["container"]
            for value in plan["bindings"].values()
            if value["role_type"] == "station"
        }
        self._mac_by_role = {
            role: value["radio_permanent_mac"].lower()
            for role, value in plan["bindings"].items()
            if value["role_type"] == "station"
        }
        # Controller display ordinals reflect discovery order and can differ
        # from the manifest's stable container/world ordinals.  BSSID
        # ownership is the authoritative bridge between both namespaces.
        self._ap_role_by_bssid: dict[str, str] = {}
        for role, value in plan["bindings"].items():
            if value["role_type"] != "fronthaul_ap":
                continue
            for radio in value.get("band_radios", {}).values():
                for interface in radio.get("interfaces", []):
                    mac = interface.get("mac")
                    if mac:
                        self._ap_role_by_bssid[mac.lower()] = role
        hero_role = manifest["hero"]["role"]
        self.hero_mac = plan["bindings"][hero_role]["radio_permanent_mac"].lower()
        self.hero_container = plan["bindings"][hero_role]["container"]

    def _optimization_subject(
        self, room: dict[str, Any] | None
    ) -> tuple[str, str, str] | None:
        """Resolve the only client eligible for this optimizer cycle."""
        hero_role = self.manifest["hero"]["role"]
        if not self.interactive:
            return hero_role, self.hero_mac, self.hero_container
        if room is None:
            return None
        role = room.get("last_rf_role")
        mac = self._mac_by_role.get(str(role))
        role_state = (room.get("roles") or {}).get(str(role), {})
        if mac is None or role_state.get("present") is not True:
            return None
        return str(role), mac, self._container_by_mac[mac]

    def _client_link_fallback(self, client, room):
        if self.profiling:
            return client
        role = self._role_by_mac.get(client.sta_mac)
        if room is None or not room.get("roles", {}).get(role, {}).get("present"):
            return client
        key = (client.sta_mac, client.connected_bssid, client.band,
               room.get("environment_epoch"), room.get("measurement_epoch"))
        with self._link_sample_lock:
            sample_lock = self._client_sample_locks.setdefault(client.sta_mac, threading.Lock())
        with sample_lock:
            checked_at, sample = self._link_samples.get(key, (float("-inf"), None))
            if time.monotonic() - checked_at >= (5 if sample else 1):
                sample = read_client_link(self._container_by_mac[client.sta_mac],
                                          client.connected_bssid, client.band,
                                          self.manifest["traffic"]["target"])
                with self._link_sample_lock:
                    self._link_samples = {cached_key: value for cached_key, value in self._link_samples.items()
                                          if cached_key[0] != client.sta_mac}
                    self._link_samples[key] = (time.monotonic(), sample)
        return replace(client, **sample) if sample else client

    def _current_metric_floor(self, client, room):
        if room is None or self.profiling:
            return None
        moved_mac = self._mac_by_role.get(room.get("last_rf_role"))
        moved_clients = {self._mac_by_role[role] for role in room.get("recent_rf_roles", [])
                         if role in self._mac_by_role}
        return room.get("last_rf_applied_at") if (
            moved_mac is None or client.sta_mac == moved_mac or client.sta_mac in moved_clients
        ) else None

    def _observation_key(self, room, *, include_generation=True):
        if self.profiling:
            return (room.get("selected_world"), self.store.world_epoch(),
                    tuple(sorted(role for role, value in room.get("roles", {}).items() if value.get("present"))),
                    room["daemon"]["instance_id"])
        return (room["revision"], room["environment_epoch"], room["daemon"]["instance_id"],
                *((room["daemon"]["generation"],) if include_generation else ()))

    def _action_window(self, now_ms: int, configured: list[int]) -> tuple[bool, str]:
        if self.interactive:
            return True, "stable_interactive_environment"
        start, end = [int(value) for value in configured]
        return start <= now_ms <= end, "scenario_time"

    def _time(self) -> int:
        return int(self.store.clock_state()[0])

    def _active(self) -> bool:
        return self.store.clock_state()[1] in {"running", "ready"}

    def _wait_for_run(self) -> bool:
        while not self.stop_event.is_set():
            if self.store.clock_state()[1] == "running":
                return True
            time.sleep(0.1)
        return False

    def _sleep(self, seconds: float) -> bool:
        return self.stop_event.wait(seconds)

    def _optimizer_wait(self, seconds: float, epoch: int | None) -> bool:
        if self.profiling:
            self._candidate_updated.wait(seconds)
            return self.stop_event.is_set()
        if not self.interactive or epoch is None:
            return self._sleep(seconds)
        return self.store.wait_environment(epoch, seconds, self.stop_event)

    def _record_error(self, worker: str, error: Exception, *, fatal: bool) -> None:
        message = f"{worker}: {type(error).__name__}: {error}"
        with self._error_lock:
            (self.errors if fatal else self.warnings).append(message)
        self.store.emit(
            "worker.error", self._time(),
            {"worker": worker, "fatal": fatal,
             "error_type": type(error).__name__, "message": str(error)},
            producer=worker,
        )

    def _client_payload(self, client) -> dict[str, Any]:
        role = self._role_by_mac.get(client.sta_mac)
        connected_role = self._ap_role_by_bssid.get(client.connected_bssid)
        if connected_role is None:
            connected_role = DEVICE_ROLES.get(client.connected_device_name.lower())
        return {
            "role": role,
            "container": self._container_by_mac.get(client.sta_mac),
            "sta_mac": client.sta_mac,
            "cohort": client.cohort,
            "ssid": client.ssid,
            "band": client.band,
            "connected_bssid": client.connected_bssid,
            "connected_device_name": client.connected_device_name,
            "connected_role": connected_role,
            "connected_world_name": client.connected_device_name or _world_device_name(connected_role),
            "rcpi": client.rcpi,
            "rssi_dbm": _rssi(client.rcpi),
            "association_uptime_seconds": client.association_uptime_seconds,
            "metric_observed_at": client.metric_observed_at,
            "measurement_source": client.measurement_source,
        }

    def _topology_payload(self, topology: dict[str, Any] | None) -> dict[str, Any]:
        """Project the controller's live mesh graph onto stable world roles.

        Controller display ordinals are discovery-order labels, so node names
        cannot safely bind an extender to a room position.  A BSS belongs to
        both the controller node and one compiled lab role; that shared BSSID
        is the authoritative bridge used for nodes, backhaul and clients.
        """
        if not topology:
            return {
                "source": "controller_topology_api",
                "available": False,
                "nodes": [],
                "backhaul_edges": [],
                "unresolved_edges": 0,
            }

        role_by_device_id: dict[str, str] = {}
        nodes = []
        for node in topology.get("nodes", []) or []:
            device_id = str(node.get("id") or "").lower()
            role = None
            for haul in node.get("haulTypes", []) or []:
                for bss in haul.get("BSSList", []) or []:
                    # RDK also lists the extender's associated backhaul BSSID
                    # as a station-mode VAP.  It is owned by the parent and
                    # must never be used to identify this controller node.
                    if bss.get("vapMode") == 1:
                        continue
                    bssid = str(bss.get("BSSID") or "").lower()
                    role = self._ap_role_by_bssid.get(bssid)
                    if role:
                        break
                if role:
                    break
            if role is None:
                role = DEVICE_ROLES.get(str(node.get("name") or "").lower())
            if not role:
                # The logical controller is intentionally not a separate
                # object in the physical room; Agent-1/gateway represents the
                # co-located controller+agent device.
                continue
            role_by_device_id[device_id] = role
            nodes.append({
                "role": role,
                "device_id": device_id,
                "name": node.get("name") or _world_device_name(role),
                "backhaul_media": node.get("backhaulMedia") or "",
                "upstream_bssid": str(node.get("upstreamBSSID") or "").lower(),
            })

        edges = []
        unresolved = 0
        for edge in topology.get("edges", []) or []:
            if str(edge.get("mediaType") or "").lower() != "wireless lan":
                continue
            parent = role_by_device_id.get(str(edge.get("from") or "").lower())
            child = role_by_device_id.get(str(edge.get("to") or "").lower())
            if not parent or not child:
                unresolved += 1
                continue
            raw_band = edge.get("band")
            try:
                band = normalize_band(raw_band)
            except ValueError:
                band = None
            signal = dict(edge.get("signal") or {})
            if signal.get("rssi_dbm") is None and edge.get("rssi") is not None:
                signal["rssi_dbm"] = edge.get("rssi")
            if signal.get("rcpi") is None and edge.get("rcpi") is not None:
                signal["rcpi"] = edge.get("rcpi")
            edges.append({
                "parent_role": parent,
                "child_role": child,
                "media_type": edge.get("mediaType") or "Wireless LAN",
                "band": band,
                "channel": edge.get("channel"),
                "upstream_bssid": str(edge.get("upstreamBSSID") or "").lower(),
                "backhaul_sta": str(edge.get("backhaulSTA") or "").lower(),
                "signal": signal,
            })

        return {
            "source": "controller_topology_api",
            "available": True,
            "nodes": sorted(nodes, key=lambda item: item["role"]),
            "backhaul_edges": sorted(
                edges, key=lambda item: (item["parent_role"], item["child_role"])
            ),
            "unresolved_edges": unresolved,
        }

    def _traffic_probe(self, room: dict[str, Any] | None = None) -> dict[str, Any]:
        if room is None:
            room = self.room_state() if self.room_state else None
        selection = (room or {}).get("traffic_probe") or {}
        role = selection.get("role") or self.manifest["hero"]["role"]
        mac = self._mac_by_role[role]
        return {
            "role": role, "selection": int(selection.get("selection", 0)),
            "sta_mac": mac, "container": self._container_by_mac[mac],
            "present": (room or {}).get("roles", {}).get(role, {}).get("present", True),
        }

    def _network_payload(
        self, snapshot, topology: dict[str, Any] | None = None,
        *, room: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clients = [self._client_payload(item) for item in snapshot.clients]
        probe = self._traffic_probe(room)
        hero = next((item for item in clients if item["sta_mac"] == probe["sta_mac"]), None)
        mesh = self._topology_payload(topology)
        applied = {(link["source_role"], link["destination_role"], link["band"]): link
                   for link in (room or {}).get("backhaul_links", [])}
        for edge in mesh.get("backhaul_edges", []):
            link = applied.get((edge["parent_role"], edge["child_role"], str(edge.get("band"))))
            channel = edge.get("channel")
            frequency = None
            if channel is not None and str(channel).isdigit():
                channel = int(channel)
                frequency = (5000 + channel * 5 if str(edge.get("band")) == "5" else
                             5950 + channel * 5 if str(edge.get("band")) == "6" else
                             2484 if channel == 14 else 2407 + channel * 5)
            if link is not None and frequency == link["frequency_mhz"]:
                edge["applied_rf"] = dict(link)
        if self.backhaul_manager is not None:
            mesh["backhaul_control"] = self.backhaul_manager.snapshot(room)
        return {
            "observed_at": snapshot.observed_at,
            "health": {
                "mesh_devices": snapshot.health.devices,
                "clients": snapshot.health.clients,
                "bsses": snapshot.health.bsses,
            },
            "cohorts": {
                "private": sum(item["cohort"] == "private" for item in clients),
                "iot": sum(item["cohort"] == "iot" for item in clients),
                "other": sum(item["cohort"] == "other" for item in clients),
            },
            "clients": clients,
            "hero": hero,
            "traffic_probe": probe,
            "mesh": mesh,
        }

    def _ping(self, container: str) -> dict[str, Any]:
        traffic = self.manifest["traffic"]
        target = str(traffic["target"])
        timeout = max(1, int(traffic["timeout_seconds"]))
        started = time.monotonic()
        command = (
            "lxc", "exec", container, "--", "ping", "-c", "1", "-W",
            str(timeout), target,
        )
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout + 2,
            )
            text = result.stdout + result.stderr
            match = re.search(r"time[=<]([0-9.]+)\s*ms", text)
            return {
                "container": container,
                "target": target,
                "success": result.returncode == 0,
                "rtt_ms": float(match.group(1)) if match else None,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                "returncode": result.returncode,
            }
        except (OSError, subprocess.TimeoutExpired) as error:
            return {
                "container": container,
                "target": target,
                "success": False,
                "rtt_ms": None,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                "error": str(error),
            }

    def profiling_contract(self) -> dict:
        room = self.room_state() if self.room_state is not None else {}
        native_observation = self.mode == "stimulus"
        return {"mode": "native-observation" if native_observation else
                "unassisted-btm" if self.profiling and self.mode == "act" else
                "external-recommendations" if self.mode == "recommend" else "assisted-demo",
                "rf_steering_assistance": self.steering_transaction is not None and self.mode == "act",
                "optimizer": "not-attributed-native-stack-or-client" if native_observation else "external-room-threshold-policy",
                "external_candidate_queries": not native_observation,
                "external_client_steering": self.mode == "act",
                "adaptive_backhaul": self.backhaul_manager is not None,
                "backhaul_rf": (room or {}).get("backhaul_policy", "scenario-plan"),
                "native_autonomous_optimizer_claim": False}

    def preflight(self) -> None:
        observer = ControllerObserver(self.base_url)
        self.coordination = observer.coordination_capabilities()
        self.store.emit("lab.coordination", 0, self.coordination, producer="conductor")
        self.store.emit("lab.profiling", 0, self.profiling_contract(), producer="conductor")
        snapshot = observer.observe()
        payload = self._network_payload(snapshot, observer.last_raw["topology"])
        hero = payload["hero"]
        expected = self.manifest["health"]
        failures = []
        if snapshot.health.devices != int(expected["expected_mesh_devices"]):
            failures.append(f"mesh devices={snapshot.health.devices}")
        if snapshot.health.clients != int(expected["expected_clients"]):
            failures.append(f"clients={snapshot.health.clients}")
        if payload["cohorts"]["private"] != int(expected["expected_private_clients"]):
            failures.append(f"private clients={payload['cohorts']['private']}")
        if payload["cohorts"]["iot"] != int(expected["expected_iot_clients"]):
            failures.append(f"IoT clients={payload['cohorts']['iot']}")
        if hero is None:
            failures.append(f"hero {self.hero_mac} is absent")
        else:
            if hero["ssid"] != self.manifest["hero"]["expected_ssid"]:
                failures.append(f"hero SSID={hero['ssid']!r}")
            if hero["band"] != self.manifest["hero"]["expected_band"]:
                failures.append(f"hero band={hero['band']!r}")
            if hero["rcpi"] is None:
                failures.append("hero RCPI is missing")
        ping = self._ping(self.hero_container)
        if not ping["success"]:
            failures.append("hero traffic probe failed")
        self.store.emit("network.snapshot", 0, payload, producer="network")
        self.store.emit("traffic.sample", 0, ping, producer="traffic")
        if failures:
            raise RuntimeError("demo preflight failed: " + "; ".join(failures))
        self.store.emit(
            "demo.state", 0,
            {"state": "ready", "mode": self.mode, "hero_mac": self.hero_mac},
            producer="conductor",
        )

    def start(self) -> None:
        workers = [
            ("network", self._network_worker),
            ("traffic", self._traffic_worker),
            ("health", self._health_worker),
            ("narrative", self._narrative_worker),
        ]
        if self.mode != "stimulus":
            workers.append(("optimizer", self._optimizer_worker))
        if self.interactive:
            workers.append(("network-metrics", self._network_metrics_worker))
        for name, target in workers:
            thread = threading.Thread(
                target=self._run_worker,
                args=(name, target),
                name=f"room-demo-{name}",
                daemon=True,
            )
            thread.start()
            self.threads.append(thread)

    def _run_worker(self, name: str, target) -> None:
        """Turn an unexpected worker exception into a failed demo result."""
        try:
            target()
        except Exception as error:  # the evidence must retain programming faults too
            self._record_error(name, error, fatal=True)

    def stop(self) -> None:
        self.stop_event.set()
        self._candidate_updated.set()
        self.store.wake()
        for thread in self.threads:
            thread.join(timeout=90)
        if self._streaming_provider is not None:
            self._streaming_provider.close()
        self._probe_executor.shutdown(wait=True)
        self._verification_executor.shutdown(wait=True)

    def _projected_association(self, station):
        with self._network_lock:
            if time.monotonic() - self._network_observed_at > 2:
                return None
            return next((client.connected_bssid for client in self._network_clients if client.sta_mac == station), None)

    def _verify_profile_action(self, verifier, decision, guard, policy_config, batch_index, batch_size,
                               action_context=None, action_started=None):
        subject_role = self._role_by_mac[decision.sta_mac]
        verifier = OutcomeVerifier(
            None, association_probe=self._projected_association,
            traffic_probe=verifier.traffic_probe, sleeper=self.stop_event.wait,
            cancelled=lambda: self.stop_event.is_set() or self.store.world_epoch() != guard[1]
            or not self.store.role_present(subject_role),
        )
        verified = verifier.verify(decision.sta_mac, decision.target_bssid,
                                   timeout_seconds=policy_config.steer_timeout_seconds, poll_seconds=0.1)
        completed_at = datetime.now(timezone.utc)
        current_world = self.store.world_epoch() == guard[1]
        with self._error_lock:
            self.verification_successes += int(verified.success and current_world)
        self.store.emit("optimizer.verification" if current_world else "optimizer.verification.discarded", self._time(), {
            **(action_context or {}),
            "request_to_verified_ms": None if action_started is None else
                round((time.monotonic() - action_started) * 1000, 3),
            **verified.to_dict(), "subject_role": self._role_by_mac[decision.sta_mac],
            "subject_mac": decision.sta_mac, "target_bssid": decision.target_bssid,
            "batch_index": batch_index, "batch_size": batch_size, "profiling": True,
        }, producer="optimizer-verifier")
        return decision, verified, completed_at, guard

    def _projected_room(self):
        if self.room_projection is not None:
            room = self.room_projection()
            if room is not None:
                return room
            current = self.store.current()
            return {"roles": current["roles"], "traffic_probe": current["traffic_probe"],
                    "projection_busy": True, "backhaul_links": []}
        return self.room_state() if self.room_state else None

    def _network_metrics_worker(self) -> None:
        if not self._wait_for_run():
            return
        room = None
        observer = ControllerObserver(
            self.base_url, max_current_metric_age_seconds=5,
            current_link_fallback=lambda client: self._client_link_fallback(client, room),
            current_metric_floor=lambda client: self._current_metric_floor(client, room),
            current_link_progress=self._publish_network_metric,
            fallback_executor=self._probe_executor,
        )
        while not self.stop_event.is_set() and self._active():
            started = time.monotonic()
            try:
                room = self.room_state() if self.room_state else None
                with self._network_lock:
                    clients = self._network_clients
                if clients:
                    observer.metrics_for(clients)
            except (OSError, ValueError, KeyError) as error:
                self._record_error("network-metrics", error, fatal=False)
            cadence = 0.25 if self.profiling else 3
            if self._sleep(max(0.05, cadence - (time.monotonic() - started))):
                break

    def _publish_network_metric(self, client):
        with self._network_lock:
            self._network_metrics[client.sta_mac] = client

    def _merge_network_metrics(self, snapshot, room):
        with self._network_lock:
            self._network_clients = snapshot.clients
            self._network_observed_at = time.monotonic()
            measured = dict(self._network_metrics)
        clients = []
        for client in snapshot.clients:
            metric = measured.get(client.sta_mac)
            floor = self._current_metric_floor(client, room)
            if (metric is not None and metric.connected_bssid == client.connected_bssid
                    and metric.band == client.band and metric.ssid == client.ssid and metric.metric_observed_at is not None
                    and (floor is None or parse_time(metric.metric_observed_at) > parse_time(floor))):
                client = replace(client, rcpi=metric.rcpi, metric_observed_at=metric.metric_observed_at,
                                 association_uptime_seconds=metric.association_uptime_seconds,
                                 measurement_source=metric.measurement_source)
            clients.append(client)
        return replace(snapshot, clients=tuple(clients))

    def _network_worker(self) -> None:
        if not self._wait_for_run():
            return
        room = None
        observer = ControllerObserver(
            self.base_url,
            current_link_fallback=(lambda client: self._client_link_fallback(client, room))
            if self.interactive and self.room_state else None,
            max_current_metric_age_seconds=10,
        )
        while not self.stop_event.is_set() and self._active():
            started = time.monotonic()
            try:
                snapshot = observer.observe_topology() if self.interactive else observer.observe()
                room = self._projected_room()
                if self.interactive:
                    snapshot = self._merge_network_metrics(snapshot, room)
                payload = self._network_payload(snapshot, observer.last_raw["topology"], room=room)
                payload["collection_seconds"] = round(time.monotonic() - started, 4)
                payload["profiling"] = self.profiling
                self.store.emit(
                    "network.snapshot", self._time(), payload,
                    producer="network",
                )
            except (OSError, ValueError, KeyError) as error:
                # The RDK libemcli adapter serializes its native command path.
                # A passive GET can time out while the bounded candidate
                # transaction owns that path; the next sample and the final
                # authoritative health gate decide whether this was transient.
                self._record_error("network", error, fatal=False)
            cadence = 0.25 if self.profiling else 1
            if self._sleep(max(0.05, cadence - (time.monotonic() - started)) if self.interactive else 2):
                break

    def _traffic_worker(self) -> None:
        if not self._wait_for_run():
            return
        interval = float(self.manifest["traffic"]["interval_seconds"])
        while not self.stop_event.is_set() and self._active():
            probe = self._traffic_probe()
            sample = self._ping(probe["container"]) if probe["present"] else {
                "container": probe["container"], "target": self.manifest["traffic"]["target"],
                "success": None, "rtt_ms": None, "status": "offline",
            }
            if self._traffic_probe() == probe:
                self.store.emit(
                    "traffic.sample", self._time(), {**sample, "traffic_probe": probe},
                    producer="traffic",
                )
            if self._sleep(interval):
                break

    def _health_worker(self) -> None:
        if not self._wait_for_run():
            return
        health = self.manifest["health"]
        interval = float(health["interval_seconds"])
        expected_devices = int(health["expected_mesh_devices"])
        expected_clients = int(health["expected_clients"])
        while not self.stop_event.is_set() and self._active():
            try:
                room = self.room_state() if self.room_state else None
                if room is not None:
                    expected_clients = int(room.get("expected_online_clients", expected_clients))
                payload = mesh_health(expected_devices, expected_clients)
                payload["pool_clients"] = int(health["expected_clients"])
                payload["expected_online_clients"] = expected_clients
                payload["healthy"] = (
                    payload.get("api_active") == expected_clients
                    and payload.get("model_devices") == expected_devices
                    and payload.get("model_radios") == expected_devices * 3
                    and payload.get("model_bsses") == expected_devices * 10
                    and payload.get("model_associated") == expected_clients + expected_devices - 1
                )
                payload["evidence_storage"] = self.store.storage_status()
                self.store.emit("health.sample", self._time(), payload, producer="health")
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                self._record_error("health", error, fatal=False)
            if self._sleep(interval):
                break

    def _narrative_worker(self) -> None:
        if not self._wait_for_run():
            return
        if self.interactive:
            label = (
                "Continuous topology reconciliation is active; move any client "
                "and the optimizer will reconverge the fleet"
                if self.mode == "act" else
                "The optimizer continuously recommends the best eligible APs"
            )
            self.store.emit(
                "demo.mark", self._time(), {"label": label, "interactive": True},
                producer="conductor",
            )
            return
        pending = list(self.manifest.get("narrative", []))
        while pending and not self.stop_event.is_set() and self._active():
            now = self._time()
            while pending and now >= int(pending[0]["time_ms"]):
                mark = pending.pop(0)
                self.store.emit(
                    "demo.mark", now,
                    {"scheduled_time_ms": int(mark["time_ms"]), "label": mark["label"]},
                    producer="conductor",
                )
            self._sleep(0.2)

    def _optimizer_worker(self) -> None:
        if not self._wait_for_run():
            return
        optimizer = self.manifest["optimizer"]
        policy_path = self.repo_root / self.manifest["policy"]
        policy_config = load_policy(policy_path)
        if self.interactive:
            # An explicitly moved client asks the room optimizer to find the
            # best eligible AP. Keep the normal gain/hysteresis gate, but do
            # not require the old association to be below the weak-link
            # threshold before measuring alternatives.
            policy_config = _interactive_policy(policy_config)
        if self.profiling:
            policy_config = replace(policy_config, require_complete_client_roster=False)
        policy = ThresholdPolicy(policy_config)
        state = PolicyState()
        priority_role = None
        priority_until = 0.0
        provider = ControllerCandidateProvider(
            self.base_url,
            max_parallel_agents=self.coordination["candidate_parallel_agents"],
            allow_simulated=bool(optimizer["allow_simulated_candidates"]),
            request_attempts=1 if self.interactive else 2,
            stop_on_unavailable=self.interactive,
            generation_guard=(lambda: self.room_state()["environment_epoch"] == room_before["environment_epoch"])
            if self.interactive and self.room_state and not self.profiling else None,
            client_selector=lambda client, observed_at: (
                (self.interactive or client.sta_mac == self.hero_mac)
                and (_candidate_measurement_needed(policy, state, client, observed_at)
                     if self.interactive else policy.requires_candidate_measurement(client, observed_at))
            ),
            client_prioritizer=lambda client, observed_at: not self.profiling and _priority_client(
                client, priority_role, self._mac_by_role, self._ap_role_by_bssid,
                priority_until, time.monotonic(),
            ),
            simulated_bss_channels=_simulated_bss_channels(self.plan),
            progress=lambda progress: self.store.emit(
                "optimizer.progress", self._time(), progress, producer="optimizer"
            ),
        )
        observer = ControllerObserver(
            self.base_url, candidate_provider=provider,
            current_link_fallback=(lambda client: self._client_link_fallback(client, room_before))
            if self.interactive and self.room_state else None,
            max_current_metric_age_seconds=10,
            current_metric_floor=(lambda client: self._current_metric_floor(client, room_before))
            if self.interactive and self.room_state else None,
            fallback_executor=self._probe_executor if self.interactive else None,
        )
        if self.profiling:
            provider = StreamingCandidateProvider(provider, maximum_clients=8,
                                                maximum_age_seconds=min(30, policy.config.reject_stale_metrics_after_seconds),
                                                identity=lambda: self._observation_key(room_before),
                                                updated=self._candidate_updated.set,
                                                telemetry=lambda value: self.store.emit(
                                                    "optimizer.collection", self._time(), value, producer="optimizer"))
            self._streaming_provider = provider
            observer.candidate_provider = provider
        verify_observer = ControllerObserver(self.base_url)
        verifier = OutcomeVerifier(
            verify_observer,
            association_probe=self._projected_association if self.profiling else (
                verify_observer.association if self.interactive else None),
            cancelled=self.stop_event.is_set,
            traffic_probe=lambda sta: self._ping(
                self._container_by_mac[sta.lower()]
            )["success"],
        )
        actuator = SteerActuator(
            self.repo_root / "gen/steer.sh",
            request_only=bool(optimizer["request_only"]),
            preview_seconds=0 if self.interactive else None,
        )
        if self.profiling:
            actuator = NativeSteerActuator(_simulated_bss_channels(self.plan),
                base_url=self.base_url if self.coordination.get("native_steering_endpoint") == "/api/v1/steer-native" else None)
        interval = float(optimizer["interval_seconds"])
        if self.profiling:
            interval = 0.25
        pending_verifications = {}
        action_window = [int(value) for value in optimizer["action_window_ms"]]
        maximum_actions = (
            int(self.maximum_actions)
            if self.maximum_actions is not None else int(optimizer["max_actions"])
        )
        observed_epoch: int | None = None
        policy_world_epoch = None
        consecutive_measurement_failures = 0
        while not self.stop_event.is_set() and self._active():
            cycle_started = time.monotonic()
            self._candidate_updated.clear()
            retry_delay = 0.0
            try:
                room_before = self.room_state() if self.room_state else None
                if self.profiling and policy_world_epoch != self.store.world_epoch():
                    policy_world_epoch = self.store.world_epoch()
                    state = PolicyState()
                    pending_verifications.clear()
                for station, future in list(pending_verifications.items()):
                    if not future.done():
                        continue
                    completed_decision, verified, completed_at, completed_guard = future.result()
                    if room_before and completed_guard == self._observation_key(room_before, include_generation=False):
                        state = _completed_action_state(state, completed_decision, policy.config, verified.success,
                                                        verified.reason, completed_at)
                    del pending_verifications[station]
                if room_before is not None:
                    epoch = int(room_before["environment_epoch"])
                    stable_for = room_before.get("stable_for_seconds")
                    if observed_epoch != epoch:
                        if not self.profiling:
                            state = PolicyState()
                        observed_epoch = epoch
                        priority_role = room_before.get("recent_rf_roles") or room_before.get("last_rf_role")
                        priority_until = time.monotonic() + CANDIDATE_PRIORITY_WINDOW_SECONDS
                        self.store.emit(
                            "optimizer.environment.observed" if self.profiling else "optimizer.environment.changed", self._time(),
                            {
                                "environment_epoch": epoch,
                                "world_revision": room_before["revision"],
                                "policy_hold_reset": not self.profiling,
                            },
                            producer="optimizer",
                        )
                    if stable_for is None:
                        self.store.emit(
                            "optimizer.measurement.waiting", self._time(),
                            {
                                "reason": "rf_not_applied",
                                "environment_epoch": epoch,
                                "world_revision": room_before["revision"],
                                "stable_for_seconds": stable_for,
                            },
                            producer="optimizer",
                        )
                        if self._optimizer_wait(0.1, observed_epoch):
                            break
                        continue
                if self.backhaul_manager is not None and room_before is not None:
                    if self.backhaul_manager.reconcile(room_before, self._time()):
                        if self._optimizer_wait(0.1, observed_epoch):
                            break
                        continue
                    if self.backhaul_manager.blocks_client_measurement(room_before):
                        self.store.emit(
                            "optimizer.measurement.waiting", self._time(),
                            {"reason": "backhaul_reconciling", "environment_epoch": observed_epoch,
                             "backhaul": self.backhaul_manager.snapshot(room_before)}, producer="optimizer",
                        )
                        if self._optimizer_wait(0.1, observed_epoch):
                            break
                        continue
                preferred_subject = self._optimization_subject(room_before)
                if preferred_subject is None:
                    hero_role = self.manifest["hero"]["role"]
                    preferred_subject = (
                        hero_role, self.hero_mac, self.hero_container
                    )
                self._candidate_active.set()
                observation_key = self._observation_key(room_before) if room_before else None
                self.store.emit(
                    "optimizer.progress", self._time(),
                    {"status": "measuring", "phase": "serving_metrics",
                     **({"environment_epoch": observed_epoch} if observed_epoch is not None else {})}, producer="optimizer",
                )
                with self._controller_lock:
                    snapshot = observer.observe()
                native_roster_clients = snapshot.health.clients
                native_roster_macs = {item.sta_mac for item in snapshot.clients}
                consecutive_measurement_failures = 0
                room_after = self.room_state() if self.room_state else None
                if room_before is not None and room_after is not None:
                    before_key = observation_key
                    after_key = self._observation_key(room_after)
                    if before_key != after_key:
                        state = PolicyState()
                        self.store.emit(
                            "observation.inconsistent_rf_epoch", self._time(),
                            {
                                "start": before_key,
                                "end": after_key,
                                "movement_active": room_after.get("movement_active"),
                            },
                            producer="optimizer",
                        )
                        continue
                    online_macs = {
                        self._mac_by_role[role] for role, value in room_after.get("roles", {}).items()
                        if value.get("present") and role in self._mac_by_role
                    }
                    online_clients = tuple(item for item in snapshot.clients if item.sta_mac in online_macs)
                    offline_macs = set(self._mac_by_role.values()) - online_macs
                    snapshot = replace(snapshot,
                        health=replace(snapshot.health, clients=snapshot.health.clients - sum(
                            item.sta_mac in offline_macs for item in snapshot.clients)),
                        clients=online_clients,
                        candidates=tuple(item for item in snapshot.candidates if item.sta_mac in online_macs))
                    policy.config = replace(
                        policy_config, expected_clients=int(room_after["expected_online_clients"])
                    )
                    applied_at = room_after.get("last_rf_applied_at")
                    if applied_at and not self.profiling:
                        applied_time = parse_time(applied_at)
                        relevant = [item for item in snapshot.clients
                                    if self._current_metric_floor(item, room_after) is not None]
                        not_fresh = [
                            item for item in relevant
                            if item.metric_observed_at is None
                            or parse_time(item.metric_observed_at) <= applied_time
                        ]
                        if not_fresh:
                            self.store.emit(
                                "optimizer.measurement.waiting", self._time(),
                                {
                                    "reason": "fresh_current_link_metric",
                                    "environment_epoch": room_after["environment_epoch"],
                                    "world_revision": room_after["revision"],
                                    "rf_applied_at": applied_at,
                                    "waiting_clients": len(not_fresh),
                                    "waiting_roles": [
                                        self._role_by_mac.get(item.sta_mac)
                                        for item in not_fresh
                                    ],
                                },
                                producer="optimizer",
                            )
                            waiting_macs = {item.sta_mac for item in not_fresh}
                            snapshot = replace(snapshot, clients=tuple(
                                replace(item, rcpi=None, metric_observed_at=None)
                                if item.sta_mac in waiting_macs else item
                                for item in snapshot.clients
                            ))
                prior = state
                evaluation = policy.evaluate(snapshot, prior)
                if not evaluation.decisions:
                    self.store.emit(
                        "optimizer.measurement.waiting", self._time(),
                        {"reason": "controller_client_roster_empty"},
                        producer="optimizer",
                    )
                    continue
                steer_decisions = [
                    item for item in evaluation.decisions if item.action == "steer"
                ]
                fleet = _fleet_status(snapshot, provider.last_selected_sta_macs & {
                    item.sta_mac for item in snapshot.clients if item.rcpi is not None
                }, policy.config.reject_stale_metrics_after_seconds, policy.config.minimum_target_gain_rcpi,
                   {self._mac_by_role[role] for role, value in room_after["roles"].items()
                    if value.get("present") and role in self._mac_by_role} if room_after else None,
                   native_roster_macs)
                ranked_steer_decisions = _ranked_action_batch(
                    steer_decisions, len(steer_decisions)
                )
                selected_action = (
                    ranked_steer_decisions[0]
                    if ranked_steer_decisions else None
                )
                preferred_role, preferred_mac, _preferred_container = preferred_subject
                preferred_decision = next(
                    (item for item in evaluation.decisions
                     if item.sta_mac == preferred_mac),
                    None,
                )
                decision = selected_action or preferred_decision or min(
                    evaluation.decisions,
                    key=lambda item: (
                        item.current_rcpi if item.current_rcpi is not None else 221,
                        item.sta_mac,
                    ),
                )
                subject_mac = decision.sta_mac
                subject_role = self._role_by_mac.get(subject_mac, preferred_role)
                subject_container = self._container_by_mac[subject_mac]
                now = self._time()
                window_open, window_kind = self._action_window(now, action_window)
                can_act = (
                    self.mode == "act"
                    and window_open
                    and self.action_attempts < maximum_actions
                )
                configured_batch_size = max(
                    1, int(optimizer.get("interactive_action_batch_size", 1))
                )
                batch_size = configured_batch_size if self.interactive else 1
                remaining_actions = max(0, maximum_actions - self.action_attempts)
                action_batch = (
                    ranked_steer_decisions[:min(batch_size, remaining_actions)]
                    if can_act else []
                )
                if self.profiling:
                    action_batch = [item for item in action_batch if item.sta_mac not in pending_verifications][
                        :max(0, 5 - len(pending_verifications))]
                if action_batch:
                    selected_action = action_batch[0]
                if steer_decisions and self.mode == "recommend":
                    state = _recommendation_state(prior, evaluation)
                elif steer_decisions and self.mode == "act" and not can_act:
                    state = (
                        _deferred_state(prior, evaluation)
                        if not window_open else _recommendation_state(prior, evaluation)
                    )
                elif selected_action is not None and can_act:
                    state = _single_action_state(
                        prior, evaluation, selected_action.sta_mac
                    )
                else:
                    state = evaluation.state
                subject_state = state.for_sta(subject_mac)
                candidates = []
                for item in snapshot.candidates_for(subject_mac):
                    if item.rcpi is None:
                        continue
                    candidate = asdict(item)
                    candidate["role"] = self._ap_role_by_bssid.get(item.bssid)
                    candidate["world_name"] = item.device_name or _world_device_name(candidate["role"])
                    candidates.append(candidate)
                self.store.emit(
                    "optimizer.evaluation", now,
                    {
                        "mode": self.mode,
                        "subject_role": subject_role,
                        "subject_mac": subject_mac,
                        "subject_container": subject_container,
                        "decision": decision.to_dict(),
                        "evaluated_at": snapshot.observed_at,
                        "minimum_dwell_seconds": policy.config.minimum_dwell_seconds,
                        "condition_hold_seconds": policy.config.condition_hold_seconds,
                        "post_steer_cooldown_seconds": policy.config.post_steer_cooldown_seconds,
                        "blocking_preview_seconds": 0 if self.interactive else 3,
                        "client_decisions": [
                            {**item, "role": self._role_by_mac.get(item["sta_mac"]),
                             "source_role": self._ap_role_by_bssid.get(item["source_bssid"]),
                             "target_role": self._ap_role_by_bssid.get(item["target_bssid"])}
                            for item in _client_optimizer_status(
                                snapshot, evaluation, policy.config,
                                provider.last_requested_sta_macs if self.profiling else provider.last_selected_sta_macs
                            )
                        ],
                        "policy_state": asdict(subject_state),
                        "candidates": candidates,
                        "candidate_transactions": len(provider.last_raw),
                        "candidate_selection": provider.last_selection,
                        "native_roster_clients": native_roster_clients,
                        "coordination": self.coordination,
                        "profiling": self.profiling,
                        "unavailable_cohort_reason": provider.last_unavailable if self.profiling else None,
                        "candidate_timings": [
                            {key: transaction.get(key) for key in ("operation", "method", "object", "radio", "query_radio",
                             "requested_at", "finished_at", "elapsed_ms", "error", "attempt", "native_admission_busy")}
                            | {"native": (transaction.get("response") or {}).get("coordination")}
                            for transaction in provider.last_raw
                        ],
                        "action_window_ms": (
                            None if self.interactive else action_window
                        ),
                        "action_window_kind": window_kind,
                        "action_window_open": window_open,
                        "automatic_actuation": self.mode == "act",
                        "automatic_actuation_ready": can_act,
                        "actuation_path": (
                            "room_serialized_rf_assist_btm"
                            if self.steering_transaction is not None else
                            "unassisted_native_btm" if self.profiling else "btm_request"
                        ),
                        "optimization_goal": "best_eligible_same_network_band_ap",
                        "minimum_target_gain_rcpi": policy.config.minimum_target_gain_rcpi,
                        "expected_online_clients": policy.config.expected_clients,
                        "partial_roster_progression": not policy.config.require_complete_client_roster,
                        "kernel_current_link_measurements": [
                            {"sta_mac": item.sta_mac, "bssid": item.connected_bssid,
                             "rssi_dbm": _rssi(item.rcpi), "observed_at": item.metric_observed_at,
                             "source": item.measurement_source}
                            for item in snapshot.clients
                            if item.measurement_source == "client_kernel_iw_link_after_wlan_traffic_probe"
                        ],
                        **({"environment_epoch": room_after["environment_epoch"]} if room_after else {}),
                        "fleet": {
                            **fleet,
                            "actionable_clients": len(steer_decisions),
                        },
                        "actions_used": self.action_attempts,
                        "maximum_actions": maximum_actions,
                        "action_batch_size": len(action_batch),
                        "action_batch_limit": batch_size,
                        "action_batch_subjects": [
                            self._role_by_mac.get(item.sta_mac, item.sta_mac)
                            for item in action_batch
                        ],
                        "observation_elapsed_ms": round(
                            (time.monotonic() - cycle_started) * 1000, 3
                        ),
                    },
                    producer="optimizer",
                )
                if action_batch:
                    batch_guard = None
                    if room_after is not None:
                        batch_guard = self._observation_key(room_after, include_generation=False)
                    for batch_index, decision in enumerate(action_batch, 1):
                        if not _action_measurements_fresh(
                            decision, snapshot, min(30, policy.config.reject_stale_metrics_after_seconds)
                            if self.profiling else policy.config.reject_stale_metrics_after_seconds,
                            datetime.now(timezone.utc),
                        ):
                            state = state.replace(_deferred_state(prior, evaluation).for_sta(decision.sta_mac))
                            self.store.emit(
                                "optimizer.batch.aborted", self._time(),
                                {"reason": "action_measurements_expired", "completed_actions": batch_index - 1,
                                 "planned_actions": len(action_batch), "subject_role": self._role_by_mac[decision.sta_mac]},
                                producer="optimizer",
                            )
                            break
                        if self.room_state is not None:
                            current_room = self.room_state()
                            current_guard = self._observation_key(current_room, include_generation=False)
                            if current_guard != batch_guard:
                                self.store.emit(
                                    "optimizer.batch.aborted", self._time(),
                                    {
                                        "reason": "room_environment_changed",
                                        "completed_actions": batch_index - 1,
                                        "planned_actions": len(action_batch),
                                        "expected": batch_guard,
                                        "actual": current_guard,
                                    },
                                    producer="optimizer",
                                )
                                break
                            # The policy initially left deferred batch members
                            # in holding state. Mark only the action that is
                            # about to be sent as pending, so an interrupted
                            # batch cannot leave unsent clients in timeout.
                            state = state.replace(
                                evaluation.state.for_sta(decision.sta_mac)
                            )
                        subject_mac = decision.sta_mac
                        subject_role = self._role_by_mac[subject_mac]
                        self.action_attempts += 1
                        action_started = time.monotonic()
                        action_context = {
                            "action_id": f"{self.store.run_id}:{self.action_attempts}",
                            "requested_at": datetime.now(timezone.utc).isoformat(),
                            "world_epoch": self.store.world_epoch(),
                            "environment_epoch": (room_after or {}).get("environment_epoch", 0),
                            "rf_applied_at": (room_after or {}).get("last_rf_applied_at"),
                            "measurement_observed_at": snapshot.observed_at,
                        }
                        self.store.emit(
                            "optimizer.action", self._time(),
                            {**action_context, "phase": "requested", "subject_role": subject_role,
                             "decision": decision.to_dict(),
                             "batch_index": batch_index,
                             "batch_size": len(action_batch),
                             "actions_used": self.action_attempts,
                             "measurement_reused": batch_index > 1},
                            producer="optimizer",
                        )
                        source_ap_role = self._ap_role_by_bssid.get(
                            decision.source_bssid
                        )
                        target_ap_role = self._ap_role_by_bssid.get(
                            decision.target_bssid or ""
                        )
                        execute_action = lambda current=decision: actuator.execute(
                            current, snapshot
                        )
                        if self.steering_transaction is not None:
                            if source_ap_role is None or target_ap_role is None:
                                raise ValueError(
                                    "steering source or target is not bound to a room AP"
                                )
                            result = self.steering_transaction(
                                subject_role,
                                source_ap_role,
                                target_ap_role,
                                decision.target_band or decision.current_band or "",
                                execute_action,
                                expected_epoch=room_after["environment_epoch"] if room_after else None,
                            )
                        else:
                            result = execute_action()
                        if result.success:
                            self.action_successes += 1
                        self.store.emit(
                            "optimizer.action", self._time(),
                            {**action_context, "phase": "submitted", "subject_role": subject_role,
                             "submission_elapsed_ms": round((time.monotonic() - action_started) * 1000, 3),
                             "decision": decision.to_dict(),
                             "result": result.to_dict(),
                             "batch_index": batch_index,
                             "batch_size": len(action_batch),
                             "actions_used": self.action_attempts,
                             "measurement_reused": batch_index > 1},
                            producer="optimizer",
                        )
                        if not result.success:
                            if self.interactive:
                                state = _completed_action_state(state, decision, policy.config, False,
                                                                "steering_request_failed", datetime.now(timezone.utc))
                            self.store.emit(
                                "optimizer.batch.aborted", self._time(),
                                {
                                    "reason": "steering_request_failed",
                                    "completed_actions": batch_index - 1,
                                    "planned_actions": len(action_batch),
                                    "subject_role": subject_role,
                                },
                                producer="optimizer",
                            )
                            break
                        if self.profiling:
                            pending_verifications[decision.sta_mac] = self._verification_executor.submit(
                                self._verify_profile_action, verifier, decision, batch_guard, policy.config,
                                batch_index, len(action_batch), action_context, action_started)
                            continue
                        verified = verifier.verify(
                            decision.sta_mac,
                            decision.target_bssid,
                            timeout_seconds=policy.config.steer_timeout_seconds,
                            poll_seconds=0.2 if self.interactive else 1,
                        )
                        if verified.success:
                            self.verification_successes += 1
                        if self.interactive:
                            state = _completed_action_state(state, decision, policy.config, verified.success,
                                                            verified.reason, datetime.now(timezone.utc))
                        self.store.emit(
                            "optimizer.verification", self._time(),
                            {
                                **verified.to_dict(),
                                **({"policy_state": asdict(state.for_sta(subject_mac))} if self.interactive else {}),
                                "subject_role": subject_role,
                                "subject_mac": subject_mac,
                                "target_bssid": decision.target_bssid,
                                "batch_index": batch_index,
                                "batch_size": len(action_batch),
                            },
                            producer="optimizer",
                        )
                        if not verified.success:
                            self.store.emit(
                                "optimizer.batch.aborted", self._time(),
                                {
                                    "reason": "verification_failed",
                                    "completed_actions": batch_index - 1,
                                    "planned_actions": len(action_batch),
                                    "subject_role": subject_role,
                                },
                                producer="optimizer",
                            )
                            break
            except InteractionError as error:
                if error.code != "environment_changed":
                    raise
                state = _interrupted_measurement_state(state)
                retry_delay = 0.1
                self.store.emit("optimizer.batch.aborted", self._time(),
                                {"reason": "room_environment_changed", "message": str(error)},
                                producer="optimizer")
            except CandidateSnapshotSuperseded as error:
                state = _interrupted_measurement_state(state)
                retry_delay = 0.1
                self.store.emit("optimizer.measurement.waiting", self._time(),
                                {"reason": "rf_snapshot_superseded", "message": str(error)},
                                producer="optimizer")
            except (CandidateMetricsUnavailable, OSError) as error:
                if not self.interactive:
                    self._record_error("optimizer", error, fatal=True)
                    return
                consecutive_measurement_failures += 1
                retry_delay = min(
                    8.0,
                    2 ** min(consecutive_measurement_failures - 1, 3),
                )
                state = _interrupted_measurement_state(state)
                self._record_error("optimizer", error, fatal=False)
                self.store.emit(
                    "optimizer.measurement.unavailable", self._time(),
                    {
                        "status": "unavailable",
                        "profiling": self.profiling,
                        "mode": self.mode,
                        "reason": "controller_transport_unavailable" if isinstance(error, OSError) else "candidate_metrics_unavailable",
                        "message": str(error),
                        "consecutive_failures": consecutive_measurement_failures,
                        "retry_delay_seconds": retry_delay,
                        "automatic_actuation": self.mode == "act",
                        "automatic_actuation_ready": False,
                        "actions_used": self.action_attempts,
                        "maximum_actions": maximum_actions,
                        "policy_hold_reset": True,
                        "fleet": {"measurement_complete": False, "converged": False},
                        "failed_transactions": [
                            transaction for transaction in provider.last_raw
                            if "error" in transaction
                        ],
                    },
                    producer="optimizer",
                )
            except (CandidateMetricsError, ValueError, KeyError) as error:
                self._record_error("optimizer", error, fatal=True)
                return
            finally:
                self._candidate_active.clear()
            elapsed = time.monotonic() - cycle_started
            if self._optimizer_wait(retry_delay or max(0.1, interval - elapsed), observed_epoch):
                break

    def summary(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "hero_role": self.manifest["hero"]["role"],
            "hero_mac": self.hero_mac,
            "action_attempts": self.action_attempts,
            "action_successes": self.action_successes,
            "verification_successes": self.verification_successes,
            "worker_errors": list(self.errors),
            "worker_warnings": list(self.warnings),
        }


def load_manifest(path: Path, repo_root: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != "easymesh.room-demo.v1":
        raise ValueError(f"{path}: unsupported room demo manifest schema")
    for key in ("world", "bindings", "policy"):
        target = repo_root / value[key]
        if not target.is_file():
            raise ValueError(f"{path}: {key} does not exist: {target}")
    return value
