from __future__ import annotations

import copy
from contextlib import nullcontext
import datetime as dt
import math
import secrets
import threading
import time
from typing import Any, Callable

from wmdcfg.actuator import ActuatorError, ControlClient
from wmdcfg.geometry import directed_link, quantize_position
from wmdcfg.runner import FREQUENCY_CAPABILITIES
from wmdcfg.world import _hash, compile_world, playback_pause_points

from .events import EventStore
from .client_wifi import parallel_disconnections, parallel_reconnections
from .recovery import RecoveryJournal


class InteractionError(RuntimeError):
    """A validated interactive-control failure suitable for an HTTP response."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


class InteractiveMediumSession:
    """Revisioned room state and the sole interactive wmediumd writer.

    The session deliberately works in stable world roles.  The existing
    configurator plan is the only component allowed to translate those roles
    into live radio identities and per-band frequencies.
    """

    def __init__(
        self,
        store: EventStore,
        world: dict[str, Any],
        layout: dict[str, Any],
        plan: dict[str, Any],
        socket_path: str,
        *,
        client_factory: Callable[[str], ControlClient] = ControlClient,
        lease_seconds: int = 30,
        minimum_update_interval: float = 0.15,
        recovery: RecoveryJournal | None = None,
        worlds: Any = None,
        disconnect_client: Callable[[str], Any] | None = None,
        reconnect_client: Callable[[str], None] | None = None,
        traffic_probe_role: str | None = None,
        adaptive_backhaul: bool = False,
        model_backhaul: bool = False,
    ) -> None:
        self.store = store
        self.world = world
        self.layout = layout
        self.plan = plan
        self.socket_path = socket_path
        self.client_factory = client_factory
        self.lease_seconds = max(10, min(120, int(lease_seconds)))
        self.minimum_update_interval = max(0.0, float(minimum_update_interval))
        self.recovery = recovery
        self.worlds = worlds
        self.adaptive_backhaul = adaptive_backhaul
        self.model_backhaul = model_backhaul
        self.disconnect_client = disconnect_client
        self.reconnect_client = reconnect_client
        self._selected_roles = set(world["roles"])
        self._selected_world = world["name"]
        self._lock = threading.RLock()
        self._client: ControlClient | None = None
        self._instance_id: str | None = None
        self._generation = 0
        self._max_updates = 0
        self._revision = 0
        self._environment_epoch = 0
        self._backhaul_epoch = 0
        self._last_backhaul_change_monotonic: float | None = None
        self._measurement_epoch = 0
        self._last_rf_apply_monotonic: float | None = None
        self._last_rf_applied_at: str | None = None
        self._last_rf_role: str | None = None
        self._recent_rf_roles: dict[str, float] = {}
        self._started_at = time.monotonic()
        self._lease: dict[str, Any] | None = None
        self._last_update_at = 0.0
        self._baseline: dict[tuple[str, str, int], tuple[int, bool]] = {}
        self._applied_values: dict[tuple[str, str, int], tuple[int, bool]] = {}
        self._protected_backhaul: dict[tuple[str, str, int], tuple[int, bool]] = {}
        self._restored = False
        self._faulted: str | None = None
        self._closing = False
        self._movements: dict[str, dict[str, Any]] = {}
        self._playback_world = copy.deepcopy(world)
        self._playback_status = "paused"
        self._playback_time_ms = 0
        self._playback_checkpoint_ms = None
        self._playback_rewind = False
        self._playback_overrides: set[str] = set()
        self._playback_wake = threading.Event()
        self._playback_thread: threading.Thread | None = None
        self._movement_threads: dict[str, threading.Thread] = {}
        self._recording: dict[str, Any] | None = None
        self._recorded_mobility: dict[str, Any] | None = None
        self._recorded_world: dict[str, Any] | None = None
        self._command_executor: Callable[..., Any] | None = None
        first = world["generations"][0]
        self._roles = {
            role: {
                "position": [float(value) for value in first["positions"][role]],
                "present": bool(first["present"][role]),
            }
            for role in sorted(world["roles"])
        }
        self._initial_roles = copy.deepcopy(self._roles)
        self._allowed_roles = tuple(
            role for role in sorted(world["roles"])
            if world["roles"][role] == "station"
        )
        self._traffic_probe_role = traffic_probe_role or next(iter(self._allowed_roles), None)
        if self._traffic_probe_role is not None and self._traffic_probe_role not in self._allowed_roles:
            raise ValueError("traffic probe must be a bound station role")
        self._traffic_probe_selection = 0
        # Extender presence is mutable so a presenter can create a reversible
        # fronthaul-service outage without stopping the container, changing
        # its identity, or disturbing its backhaul/control-plane state. The
        # gateway is deliberately excluded from this bounded demo control.
        self._presence_roles = tuple(
            role for role in sorted(world["roles"])
            if world["roles"][role] == "station"
            or (world["roles"][role] == "fronthaul_ap" and role != "gateway")
        )
        # Moving the gateway means moving the colocated Agent-1 radios in the
        # RF room.  The controller process and WAN remain where they are, and
        # gateway presence remains protected; only its fronthaul/backhaul
        # geometry is mutable.
        self._movable_roles = tuple(
            role for role in sorted(world["roles"])
            if world["roles"][role] in {"station", "fronthaul_ap"}
        )
        layout_nodes = {item["role"]: dict(item) for item in layout.get("nodes", [])}
        self._nodes = {
            role: {"role": role, **layout_nodes.get(role, {})}
            for role in world["roles"]
        }
        if self.worlds is not None:
            self._nodes = self.worlds.rf_nodes(world, layout)

    def set_command_executor(self, executor: Callable[..., Any]) -> None:
        """Route autonomous movement ticks through the owning RoomEngine."""
        self._command_executor = executor

    def _world_time(self) -> int:
        return max(0, round((time.monotonic() - self._started_at) * 1000))

    def world_time(self) -> int:
        """Return this session's monotonic run time for actor-owned events."""
        return self._world_time()

    @staticmethod
    def runtime_world(world: dict[str, Any], layout: dict[str, Any]) -> dict[str, Any]:
        """Add the source geometry needed by a live viewer without resigning a Golden World."""
        result = copy.deepcopy(world)
        result["space"] = copy.deepcopy(layout["space"])
        result["propagation"] = copy.deepcopy(layout["propagation"])
        result["interaction"] = {
            "authoritative": True,
            "position_url": "/api/demo/roles/{role}/position",
            "presence_url": "/api/demo/roles/{role}/presence",
            "playback_url": "/api/demo/playback",
            "move_url": "/api/demo/roles/{role}/move",
            "movement_url": "/api/demo/movements/{movement}",
        }
        return result

    def start(self) -> None:
        with self._lock:
            if self._client is not None:
                raise InteractionError(409, "already_started", "interactive session is already started")
            client = self.client_factory(self.socket_path)
            recovery_prepared = False
            try:
                status = client.connect()
                missing = FREQUENCY_CAPABILITIES - status.capabilities
                if missing:
                    raise ActuatorError(
                        f"daemon lacks interactive capabilities {sorted(missing)}"
                    )
                self._client = client
                self._instance_id = status.instance_id
                self._generation = status.generation
                self._max_updates = status.max_updates
                self._started_at = time.monotonic()
                # Apply the complete room, including AP-to-AP geometry.  A
                # later extender move and Reset role must return to the same
                # room state that was active at session start, not introduce
                # backhaul modeling only after the first move.  Several role
                # calculations touch the same fronthaul/AP-peer key, so reduce
                # them to one deterministic atomic update.
                initial_by_key: dict[
                    tuple[str, str, int], dict[str, Any]
                ] = {}
                for role in self._allowed_roles:
                    updates, _ = self._links_for_role(role)
                    for item in updates:
                        key = (
                            item["source"], item["destination"],
                            int(item["frequency_mhz"]),
                        )
                        initial_by_key[key] = item
                for role in self._movable_roles:
                    if self.world["roles"][role] != "fronthaul_ap":
                        continue
                    updates, _ = self._extender_links(
                        role, include_backhaul=True
                    )
                    for item in updates:
                        key = (
                            item["source"], item["destination"],
                            int(item["frequency_mhz"]),
                        )
                        initial_by_key[key] = item
                initial_updates = list(initial_by_key.values())
                if len(initial_updates) > status.max_updates:
                    raise ActuatorError(
                        f"initial room requires {len(initial_updates)} updates, "
                        f"daemon limit is {status.max_updates}"
                    )
                self._capture_baseline(initial_updates)
                if self.recovery is not None:
                    self.recovery.prepare(
                        self._instance_id, self._generation, self._baseline
                    )
                    recovery_prepared = True
                applied = self._apply_generation(initial_updates)
                for item in applied:
                    _, value, overridden = client.get_frequency_link(
                        item["source"], item["destination"], item["frequency_mhz"]
                    )
                    if value != item["value"] or overridden != item["override"]:
                        raise ActuatorError("initial interactive generation readback mismatch")
                    key = (item["source"], item["destination"], item["frequency_mhz"])
                    self._applied_values[key] = (item["value"], item["override"])
                if self.worlds is not None and not self.adaptive_backhaul and not self.model_backhaul:
                    mesh_macs = {
                        radio["tx_mac"] for binding in self.plan["bindings"].values()
                        if binding["role_type"] == "fronthaul_ap"
                        for radio in binding.get("band_radios", {}).values()
                    }
                    mesh_macs.update(binding["radio_tx_mac"] for binding in self.plan["bindings"].values()
                                     if binding["role_type"] == "fronthaul_ap")
                    self._protected_backhaul = {
                        key: value for key, value in self._applied_values.items()
                        if key[0] in mesh_macs and key[1] in mesh_macs
                    }
                self._mark_rf_committed()
                self._emit_playback("loaded")
                self.store.emit(
                    "rf.generation.applied", 0,
                    {
                        "revision": 0,
                        "role": None,
                        "cause": "interactive-initial-state",
                        "daemon_instance_id": self._instance_id,
                        "daemon_generation": self._generation,
                        "changed_link_count": len(applied),
                        "environment_epoch": self._environment_epoch,
                    },
                    producer="interaction",
                )
                self.store.emit(
                    "interaction.session.ready", 0,
                    {
                        "revision": self._revision,
                        "allowed_roles": list(self._allowed_roles),
                        "presence_roles": list(self._presence_roles),
                        "movable_roles": list(self._movable_roles),
                        "daemon_instance_id": status.instance_id,
                        "daemon_generation": self._generation,
                        "environment_epoch": self._environment_epoch,
                    },
                    producer="interaction",
                )
            except Exception:
                if (
                    self._client is not None
                    and self._baseline
                    and (self.recovery is None or recovery_prepared)
                ):
                    try:
                        self.restore()
                    except Exception:
                        pass
                client.close()
                self._client = None
                raise

    def _expire_lease(self) -> None:
        if self._lease is None or self._lease["expires_monotonic"] > time.monotonic():
            return
        owner = self._lease["owner"]
        token = self._lease["token"]
        self._lease = None
        self._pause_playback("lease_expired")
        self._cancel_owned_movements(token, "lease_expired")
        self.store.emit(
            "interaction.lease.expired", self._world_time(), {"owner": owner},
            producer="interaction",
        )

    def projection_snapshot(self) -> dict[str, Any] | None:
        if not self._lock.acquire(blocking=False):
            return None
        try:
            return self.snapshot(expire_lease=False)
        finally:
            self._lock.release()

    def snapshot(self, *, expire_lease: bool = True) -> dict[str, Any]:
        with self._lock:
            if expire_lease:
                self._expire_lease()
            lease = None if self._lease is None else {
                "held": True,
                "owner": self._lease["owner"],
                "expires_at": self._lease["expires_at"],
            }
            return {
                "schema": "easymesh.room-demo.interactions.v1",
                "enabled": self._client is not None,
                "revision": self._revision,
                "environment_epoch": self._environment_epoch,
                "measurement_epoch": self._measurement_epoch,
                "backhaul_epoch": self._backhaul_epoch,
                "backhaul_stable_for_seconds": (
                    None if self._last_backhaul_change_monotonic is None else
                    round(time.monotonic() - self._last_backhaul_change_monotonic, 3)
                ),
                "last_rf_applied_at": self._last_rf_applied_at,
                "last_rf_role": self._last_rf_role,
                "recent_rf_roles": sorted(role for role, changed in self._recent_rf_roles.items()
                                          if time.monotonic() - changed < 120),
                "stable_for_seconds": (
                    None if self._last_rf_apply_monotonic is None else
                    round(time.monotonic() - self._last_rf_apply_monotonic, 3)
                ),
                "movement_active": self._playback_status == "playing" or any(
                    value["status"] in {"running", "paused"}
                    for value in self._movements.values()
                ),
                "playback": self._public_playback(),
                "traffic_probe": self._traffic_probe(),
                "allowed_roles": sorted(set(self._allowed_roles) & self._selected_roles),
                "presence_roles": sorted(set(self._presence_roles) & self._selected_roles),
                "movable_roles": sorted(set(self._movable_roles) & self._selected_roles),
                "world_switch_enabled": self.worlds is not None,
                "backhaul_policy": self.backhaul_policy(),
                "backhaul_links": self.backhaul_links(),
                "selected_world": self._selected_world,
                "pool_clients": len(self._allowed_roles),
                "expected_online_clients": sum(self._roles[role]["present"] for role in self._allowed_roles),
                "roles": copy.deepcopy(self._roles),
                "lease": lease or {"held": False},
                "daemon": {
                    "instance_id": self._instance_id,
                    "generation": self._generation,
                },
                "movements": [
                    self._public_movement(value)
                    for value in sorted(
                        self._movements.values(), key=lambda item: item["created_monotonic"]
                    )
                ],
                "recording": self._recording_status(),
                "restored": self._restored,
                "fault": self._faulted,
                "recovery": None if self.recovery is None else self.recovery.snapshot(),
            }

    def acquire(self, owner: str) -> dict[str, Any]:
        owner = str(owner or "").strip()[:80]
        if not owner:
            raise InteractionError(400, "invalid_owner", "lease owner is required")
        with self._lock:
            self._expire_lease()
            if self._lease is not None:
                raise InteractionError(
                    409, "lease_held",
                    f"interactive control is held by {self._lease['owner']}",
                )
            token = secrets.token_urlsafe(24)
            expires = time.monotonic() + self.lease_seconds
            expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
                seconds=self.lease_seconds
            )
            self._lease = {
                "token": token,
                "owner": owner,
                "expires_monotonic": expires,
                "expires_at": expires_at.isoformat(),
            }
            self.store.emit(
                "interaction.lease.acquired", self._world_time(),
                {"owner": owner, "expires_at": expires_at.isoformat()},
                producer="interaction",
            )
            return {
                "token": token,
                "owner": owner,
                "expires_at": expires_at.isoformat(),
                "lease_seconds": self.lease_seconds,
                "revision": self._revision,
            }

    def renew(self, token: str) -> dict[str, Any]:
        with self._lock:
            lease = self._require_lease(token)
            expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
                seconds=self.lease_seconds
            )
            lease["expires_monotonic"] = time.monotonic() + self.lease_seconds
            lease["expires_at"] = expires_at.isoformat()
            return {
                "owner": lease["owner"],
                "expires_at": expires_at.isoformat(),
                "lease_seconds": self.lease_seconds,
                "revision": self._revision,
            }

    def release(self, token: str) -> dict[str, Any]:
        with self._lock:
            lease = self._require_lease(token)
            owner = lease["owner"]
            self._lease = None
            self._pause_playback("lease_released")
            self._cancel_owned_movements(token, "lease_released")
            self.store.emit(
                "interaction.lease.released", self._world_time(), {"owner": owner},
                producer="interaction",
            )
            return {"released": True, "revision": self._revision}

    def _require_lease(self, token: str) -> dict[str, Any]:
        self._expire_lease()
        if self._lease is None:
            raise InteractionError(409, "lease_required", "interactive control lease is required")
        if not secrets.compare_digest(str(token or ""), self._lease["token"]):
            raise InteractionError(403, "lease_mismatch", "interactive control lease does not match")
        return self._lease

    def _validate_mutation(
        self,
        role: str,
        token: str,
        expected_revision: Any,
        *,
        allowed_roles: tuple[str, ...] | None = None,
        require_selected: bool = True,
    ) -> None:
        if self._client is None:
            raise InteractionError(503, "not_started", "interactive medium is unavailable")
        if self._faulted is not None:
            raise InteractionError(
                503, "session_faulted",
                f"interactive medium was restored after an actuator failure: {self._faulted}",
            )
        self._require_lease(token)
        if (require_selected and role not in self._selected_roles) or role not in (self._allowed_roles if allowed_roles is None else allowed_roles):
            raise InteractionError(400, "role_not_interactive", f"role {role!r} is not interactive")
        try:
            revision = int(expected_revision)
        except (TypeError, ValueError) as error:
            raise InteractionError(400, "invalid_revision", "expected_revision is required") from error
        if revision != self._revision:
            raise InteractionError(
                409, "stale_revision",
                f"expected revision {revision}, current revision is {self._revision}",
            )

    def _traffic_probe(self) -> dict[str, Any]:
        return {"role": self._traffic_probe_role, "selection": self._traffic_probe_selection}

    def select_traffic_probe(
        self, role: str, *, token: str, expected_revision: Any
    ) -> dict[str, Any]:
        with self._lock:
            self._validate_mutation(role, token, expected_revision, require_selected=False)
            if role != self._traffic_probe_role:
                self._traffic_probe_role = role
                self._traffic_probe_selection += 1
                self._revision += 1
            result = {"revision": self._revision, "traffic_probe": self._traffic_probe()}
            self.store.emit("traffic.probe.selected", self._world_time(), result, producer="interaction")
            return result

    def world_catalog(self) -> dict[str, Any]:
        if self.worlds is None:
            return {"enabled": False, "worlds": []}
        return {"enabled": True, **self.worlds.catalog()}

    def _room_updates(self) -> list[dict[str, Any]]:
        by_key = {}
        for role in self._movable_roles:
            updates, _ = self._links_for_role(role)
            for item in updates:
                by_key[(item["source"], item["destination"], item["frequency_mhz"])] = item
        return list(by_key.values())

    def apply_world(
        self, selection: Any, *, token: str, expected_revision: Any
    ) -> dict[str, Any]:
        with self._lock:
            self._validate_mutation(
                "gateway", token, expected_revision, allowed_roles=self._movable_roles
            )
            if self.worlds is None:
                raise InteractionError(409, "world_switch_unavailable", "world switching is not configured")
            world, layout = self.worlds.select(selection)
            if self._recording is not None:
                raise InteractionError(409, "recording_active", "stop and download the recording before changing worlds")
            self._pause_playback("world_changed")
            previous = (self.world, self.layout, self._roles, self._nodes,
                        self._selected_roles, self._selected_world, self._initial_roles)
            prior_values = dict(self._applied_values)
            first = world["generations"][0]
            roles = {}
            for role in self.worlds.roles:
                roles[role] = {
                    "position": list(first["positions"].get(role, [0, 0])),
                    "present": bool(first["present"].get(role, False)),
                }
            expanded = copy.deepcopy(world)
            expanded["roles"] = dict(self.worlds.roles)
            expanded["generations"] = [{**copy.deepcopy(first),
                "positions": {role: state["position"] for role, state in roles.items()},
                "present": {role: state["present"] for role, state in roles.items()}}]
            nodes = self.worlds.rf_nodes(world, layout)
            self.world, self.layout, self._roles = expanded, layout, roles
            self._nodes = {role: {"role": role, **nodes.get(role, {})} for role in roles}
            attempted = False
            coordination_started = time.monotonic()
            apply_timing = {}
            try:
                updates = self._room_updates()
                if any((item["source"], item["destination"], item["frequency_mhz"]) not in self._baseline for item in updates):
                    raise InteractionError(422, "world_changes_radios", "world requires RF keys outside the captured lab pool")
                changed = [item for item in updates if prior_values.get(
                    (item["source"], item["destination"], item["frequency_mhz"])
                ) != (item["value"], item["override"])]
                for movement in self._movements.values():
                    self._cancel_movement(movement, "world_changed")
                attempted = True
                with parallel_disconnections(
                    self.disconnect_client(role) for role in self._allowed_roles
                    if self.disconnect_client is not None and not roles[role]["present"] and previous[2][role]["present"]
                ):
                    disconnected_at = time.monotonic()
                    apply_timing["disconnect_ms"] = round((disconnected_at - coordination_started) * 1000, 3)
                    requested = changed or updates
                    applied = self._apply_generation(requested)
                    if applied != requested:
                        raise ActuatorError("world apply count or contents mismatch")
                    for item in applied:
                        key = (item["source"], item["destination"], item["frequency_mhz"])
                        _, value, overridden = self._client.get_frequency_link(*key)
                        if (value, overridden) != (item["value"], item["override"]):
                            raise ActuatorError("world RF readback mismatch")
                        self._applied_values[key] = (value, overridden)
                    apply_timing["medium_apply_readback_ms"] = round((time.monotonic() - disconnected_at) * 1000, 3)
                reconnect_started = time.monotonic()
                parallel_reconnections(self.reconnect_client, (
                    role for role in self._allowed_roles if self.reconnect_client is not None and roles[role]["present"]
                ))
                apply_timing["reconnect_ms"] = round((time.monotonic() - reconnect_started) * 1000, 3)
                apply_timing["total_ms"] = round((time.monotonic() - coordination_started) * 1000, 3)
                apply_timing["client_control_parallelism"] = 4
            except Exception as error:
                (self.world, self.layout, self._roles, self._nodes,
                 self._selected_roles, self._selected_world, self._initial_roles) = previous
                if attempted and not self._faulted:
                    try:
                        rollback = [{"source": key[0], "destination": key[1],
                                     "frequency_mhz": key[2], "value": value,
                                     "override": overridden}
                                    for key, (value, overridden) in prior_values.items()]
                        self._apply_generation(rollback)
                        for key, expected in prior_values.items():
                            _, value, overridden = self._client.get_frequency_link(*key)
                            if (value, overridden) != expected:
                                raise ActuatorError("world rollback readback mismatch")
                        self._applied_values = prior_values
                        self._mark_rf_committed()
                        for role in self._allowed_roles:
                            if self._roles[role]["present"] and self.reconnect_client is not None:
                                self.reconnect_client(role)
                            elif not self._roles[role]["present"] and self.disconnect_client is not None:
                                with self.disconnect_client(role):
                                    isolated, _ = self._station_links(role)
                                    self._write_verified(isolated)
                    except Exception as rollback_error:
                        self._faulted = self._faulted or str(rollback_error)
                        raise ActuatorError(f"world switch failed: {error}; rollback failed: {rollback_error}") from error
                raise
            self._selected_roles = set(world["roles"])
            self._selected_world = world["name"]
            self._initial_roles = copy.deepcopy(roles)
            self._playback_world = copy.deepcopy(world)
            self._playback_status = "paused"
            self._playback_time_ms = 0
            self._playback_checkpoint_ms = None
            self._playback_rewind = False
            self._playback_overrides.clear()
            self._revision += 1
            self._mark_rf_committed()
            runtime = self.runtime_world(world, layout)
            runtime["generations"] = [copy.deepcopy(first)]
            runtime["interaction"]["initial_frame_only"] = True
            runtime["interaction"]["backhaul_policy"] = self.backhaul_policy()
            payload = {"revision": self._revision, "world": runtime,
                       "apply_timing": apply_timing,
                       "roles": copy.deepcopy(roles), "environment_epoch": self._environment_epoch,
                       "pool_clients": len(self._allowed_roles),
                       "expected_online_clients": sum(roles[role]["present"] for role in self._allowed_roles),
                       "daemon_instance_id": self._instance_id,
                       "daemon_generation": self._generation, "changed_link_count": len(changed)}
            self.store.emit("room.world.committed", self._world_time(), payload, producer="interaction")
            self._emit_playback("loaded")
            self.store.emit("rf.generation.applied", self._world_time(), {
                key: payload[key] for key in ("revision", "environment_epoch", "daemon_instance_id",
                                             "daemon_generation", "changed_link_count")
            }, producer="interaction")
            return {key: value for key, value in payload.items() if key not in {"world", "roles"}}

    def _write_verified(self, updates: list[dict[str, Any]]) -> None:
        applied = self._apply_generation(updates)
        if applied != updates:
            raise ActuatorError("RF update count or contents mismatch")
        for item in applied:
            key = (item["source"], item["destination"], item["frequency_mhz"])
            _, value, overridden = self._client.get_frequency_link(*key)
            if (value, overridden) != (item["value"], item["override"]):
                raise ActuatorError("RF update readback mismatch")
            self._applied_values[key] = (value, overridden)

    def _public_playback(self) -> dict[str, Any]:
        return {"enabled": True, "status": self._playback_status,
                "time_ms": self._playback_time_ms,
                "duration_ms": self._playback_world["duration_ms"],
                "checkpoint_ms": self._playback_checkpoint_ms,
                "manual_roles": sorted(self._playback_overrides), "interval_ms": 1000}

    def _emit_playback(self, action: str, reason: str | None = None) -> dict[str, Any]:
        payload = {"revision": self._revision, "playback": self._public_playback(),
                   "roles": copy.deepcopy(self._roles),
                   "environment_epoch": self._environment_epoch,
                   "daemon_generation": self._generation}
        if reason is not None:
            payload["reason"] = reason
        self.store.emit(f"interaction.playback.{action}", self._world_time(), payload,
                        producer="interaction")
        return payload

    def _pause_playback(self, reason: str) -> None:
        if self._playback_status == "playing":
            self._playback_status = "paused"
            self._playback_checkpoint_ms = self._playback_time_ms if reason == "scenario_checkpoint" else None
            self._revision += 1
            self._emit_playback("paused", reason)
        self._playback_wake.set()

    def _validate_playback(self) -> None:
        try:
            playback_pause_points(self._playback_world)
        except ValueError as error:
            raise InteractionError(422, "invalid_playback", str(error)) from error
        previous_time = -1
        roles = set(self._playback_world["roles"])
        if not self._playback_world["generations"] or self._playback_world["generations"][0].get("time_ms") != 0:
            raise InteractionError(422, "invalid_playback", "playback must start at time zero")
        for frame in self._playback_world["generations"]:
            frame_time = frame.get("time_ms")
            if (not isinstance(frame_time, int) or isinstance(frame_time, bool)
                    or not previous_time < frame_time <= self._playback_world["duration_ms"]
                    or set(frame.get("positions", {})) != roles
                    or set(frame.get("present", {})) != roles):
                raise InteractionError(422, "invalid_playback", "playback requires ordered, complete room frames")
            for role in roles:
                self._clamp_position(frame["positions"][role])
                if not isinstance(frame["present"][role], bool):
                    raise InteractionError(422, "invalid_playback", "playback presence must be boolean")
            if not frame["present"].get("gateway"):
                raise InteractionError(422, "invalid_playback", "playback cannot disable the gateway")
            previous_time = frame_time

    def playback_control(self, action: str, *, token: str, expected_revision: Any) -> dict[str, Any]:
        with self._lock:
            self._validate_mutation("gateway", token, expected_revision, allowed_roles=self._movable_roles)
            if action not in {"play", "pause"}:
                raise InteractionError(400, "invalid_playback_action", "use play or pause")
            if action == "play":
                self._validate_playback()
                if self._playback_status == "completed":
                    self._playback_time_ms = 0
                    self._playback_rewind = True
                self._playback_status = "playing"
            else:
                self._playback_status = "paused"
            self._playback_checkpoint_ms = None
            self._revision += 1
            result = self._emit_playback("started" if action == "play" else "paused")
            if action == "play" and self._playback_thread is None:
                self._playback_thread = threading.Thread(target=self._playback_worker,
                                                        name="room-demo-playback", daemon=True)
                self._playback_thread.start()
            self._playback_wake.set()
            return result

    def _playback_tick(self) -> None:
        with self._lock:
            self._expire_lease()
            if self._closing or self._playback_status != "playing" or self._lease is None:
                return
            frames = self._playback_world["generations"]
            target_time = min(self._playback_world["duration_ms"], self._playback_time_ms + 1000)
            if self._playback_rewind:
                target_time = 0
            else:
                for previous, upcoming in zip(frames, frames[1:]):
                    if (self._playback_time_ms < upcoming["time_ms"] <= target_time
                            and previous["present"] != upcoming["present"]):
                        target_time = upcoming["time_ms"]
                        break
                checkpoint = next((when for when in self._playback_world.get("pause_at_ms", [])
                                   if self._playback_time_ms < when <= target_time), None)
                if checkpoint is not None:
                    target_time = checkpoint
            current = next(frame for frame in reversed(frames) if frame["time_ms"] <= target_time)
            following = next((frame for frame in frames if frame["time_ms"] > target_time), None)
            previous_roles = copy.deepcopy(self._roles)
            changed_roles = []
            started = time.monotonic()
            try:
                for role in sorted(self._selected_roles - self._playback_overrides):
                    point = current["positions"][role]
                    if following and current["present"][role] and following["present"][role]:
                        fraction = (target_time - current["time_ms"]) / (following["time_ms"] - current["time_ms"])
                        point = [point[axis] + fraction * (following["positions"][role][axis] - point[axis])
                                 for axis in (0, 1)]
                    desired = {"position": self._clamp_position(point), "present": current["present"][role]}
                    changes = ["position", "presence"] if desired["present"] else ["presence", "position"]
                    for change in changes:
                        field = "present" if change == "presence" else "position"
                        if desired[field] == self._roles[role][field]:
                            continue
                        self._roles[role][field] = desired[field]
                        changed_roles.append((role, change))
                if changed_roles:
                    self._apply_roles(changed_roles, client_sequence=None, publish_roles=False)
            except Exception as error:
                if not self._faulted or self._faulted.startswith("unexpected external medium change:"):
                    self._roles = previous_roles
                self._pause_playback(str(error))
                return
            self.store.emit("playback.rf.applied", target_time, {
                "roles": sorted({role for role, _change in changed_roles}),
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                "daemon_generation": self._generation,
            }, producer="interaction")
            self._playback_time_ms = target_time
            self._playback_rewind = False
            if target_time in self._playback_world.get("pause_at_ms", []):
                self._pause_playback("scenario_checkpoint")
                return
            if target_time >= self._playback_world["duration_ms"]:
                self._playback_status = "completed"
            self._emit_playback("progress" if self._playback_status == "playing" else "completed")

    def _playback_worker(self) -> None:
        next_tick = time.monotonic() + 1
        while not self._closing:
            if self._playback_wake.wait(max(0, next_tick - time.monotonic())):
                self._playback_wake.clear()
                next_tick = time.monotonic() + 1
                continue
            if self._closing:
                return
            next_tick = time.monotonic() + 1
            try:
                if self._command_executor is None:
                    self._playback_tick()
                else:
                    self._command_executor(self._playback_tick)
            except RuntimeError as error:
                if "room engine is closing" in str(error):
                    return
                raise

    def _clamp_position(self, value: Any) -> list[float]:
        if not isinstance(value, list) or len(value) != 2:
            raise InteractionError(400, "invalid_position", "position must be [x, y]")
        try:
            point = [float(value[0]), float(value[1])]
        except (TypeError, ValueError) as error:
            raise InteractionError(400, "invalid_position", "position must contain numbers") from error
        if not all(math.isfinite(item) for item in point):
            raise InteractionError(400, "invalid_position", "position must contain finite numbers")
        width = float(self.layout["space"]["width_m"])
        height = float(self.layout["space"]["height_m"])
        if not 0 <= point[0] <= width or not 0 <= point[1] <= height:
            raise InteractionError(
                400, "outside_room",
                f"position must be inside 0..{width:g} by 0..{height:g} metres",
            )
        quantized = quantize_position(point, quantum_m=0.05)
        return [quantized[0], quantized[1]]

    def position(
        self,
        role: str,
        *,
        token: str,
        expected_revision: Any,
        position: Any,
        final: bool = False,
        client_sequence: Any = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._validate_mutation(
                role, token, expected_revision, allowed_roles=self._movable_roles
            )
            self._cancel_role_movement(role, "direct_position")
            now = time.monotonic()
            if not final and now - self._last_update_at < self.minimum_update_interval:
                raise InteractionError(429, "rate_limited", "position updates are limited to five per second")
            point = self._clamp_position(position)
            previous = copy.deepcopy(self._roles[role])
            self._roles[role]["position"] = point
            try:
                result = self._apply_role(role, "position", client_sequence=client_sequence)
            except Exception:
                if self._faulted is None or self._faulted.startswith(
                    "unexpected external medium change:"
                ):
                    self._roles[role] = previous
                raise
            self._last_update_at = now
            self._playback_overrides.add(role)
            return result

    def presence(
        self,
        role: str,
        *,
        token: str,
        expected_revision: Any,
        present: Any,
        client_sequence: Any = None,
    ) -> dict[str, Any]:
        if not isinstance(present, bool):
            raise InteractionError(400, "invalid_presence", "present must be true or false")
        with self._lock:
            self._validate_mutation(
                role, token, expected_revision, allowed_roles=self._presence_roles
            )
            if not present:
                self._cancel_role_movement(role, "role_absent")
            previous = copy.deepcopy(self._roles[role])
            self._roles[role]["present"] = present
            try:
                result = self._apply_role(role, "presence", client_sequence=client_sequence)
                self._playback_overrides.add(role)
                return result
            except Exception:
                if self._faulted is None or self._faulted.startswith(
                    "unexpected external medium change:"
                ):
                    self._roles[role] = previous
                raise

    @staticmethod
    def _public_movement(movement: dict[str, Any]) -> dict[str, Any]:
        return {
            key: copy.deepcopy(value)
            for key, value in movement.items()
            if key not in {"lease_token", "wake", "created_monotonic", "started_monotonic",
                           "paused_monotonic", "paused_seconds"}
        }

    def _cancel_movement(self, movement: dict[str, Any], reason: str) -> None:
        if movement["status"] not in {"running", "paused"}:
            return
        movement["status"] = "cancelled"
        movement["reason"] = reason
        movement["position"] = list(self._roles[movement["role"]]["position"])
        movement["wake"].set()
        self.store.emit(
            "interaction.movement.cancelled", self._world_time(),
            {"revision": self._revision, "reason": reason,
             "movement": self._public_movement(movement)},
            producer="interaction",
        )

    def _cancel_role_movement(self, role: str, reason: str) -> None:
        for movement in self._movements.values():
            if movement["role"] == role:
                self._cancel_movement(movement, reason)

    def _cancel_owned_movements(self, token: str, reason: str) -> None:
        for movement in self._movements.values():
            if movement.get("lease_token") == token:
                self._cancel_movement(movement, reason)

    def _recording_status(self) -> dict[str, Any]:
        if self._recording is None:
            status = {
                "active": False,
                "export_ready": self._recorded_world is not None,
            }
            if self._recorded_mobility is not None:
                status.update({
                    "name": self._recorded_mobility["name"],
                    "duration_ms": self._recorded_mobility["duration_ms"],
                })
            return status
        frames = sum(len(value) for value in self._recording["frames"].values())
        return {
            "active": True,
            "export_ready": False,
            "name": self._recording["name"],
            "started_at": self._recording["started_at"],
            "duration_ms": self._recording_time(),
            "keyframes": frames,
        }

    def _recording_time(self) -> int:
        if self._recording is None:
            return 0
        return max(
            0,
            round((time.monotonic() - self._recording["started_monotonic"]) * 1000),
        )

    def _record_frame(self, role: str) -> None:
        if self._recording is None:
            return
        frames = self._recording["frames"][role]
        frame = {
            "time_ms": self._recording_time(),
            "position": list(self._roles[role]["position"]),
            "present": bool(self._roles[role]["present"]),
        }
        if frames and frame["time_ms"] == frames[-1]["time_ms"]:
            frames[-1] = frame
        elif not frames or (
            frame["position"] != frames[-1]["position"]
            or frame["present"] != frames[-1]["present"]
        ):
            frames.append(frame)

    def start_recording(
        self, *, token: str, expected_revision: Any, name: Any = None
    ) -> dict[str, Any]:
        with self._lock:
            self._require_lease(token)
            self._validate_control_revision(expected_revision)
            if self._recording is not None:
                raise InteractionError(409, "recording_active", "a recording is already active")
            raw_name = str(name or "interactive-room").strip()
            clean_name = "".join(
                character if character.isalnum() or character in "-_" else "-"
                for character in raw_name
            ).strip("-")[:80]
            if not clean_name:
                raise InteractionError(400, "invalid_recording_name", "recording name is empty")
            now = dt.datetime.now(dt.timezone.utc)
            self._recording = {
                "name": clean_name,
                "started_at": now.isoformat(),
                "started_monotonic": time.monotonic(),
                "frames": {
                    role: [{
                        "time_ms": 0,
                        "position": list(self._roles[role]["position"]),
                        "present": bool(self._roles[role]["present"]),
                    }]
                    for role in self._movable_roles if role in self._selected_roles
                },
            }
            self._recorded_mobility = None
            self._recorded_world = None
            payload = {"revision": self._revision, "recording": self._recording_status()}
            self.store.emit(
                "interaction.recording.started", self._world_time(), payload,
                producer="interaction",
            )
            return payload

    def _validate_control_revision(self, expected_revision: Any) -> int:
        try:
            revision = int(expected_revision)
        except (TypeError, ValueError) as error:
            raise InteractionError(
                400, "invalid_revision", "expected_revision is required"
            ) from error
        if revision > self._revision:
            raise InteractionError(
                409, "stale_revision",
                f"expected revision {revision} is ahead of current revision {self._revision}",
            )
        return revision

    @staticmethod
    def _presence_intervals(
        frames: list[dict[str, Any]], duration_ms: int
    ) -> list[list[int]]:
        intervals: list[list[int]] = []
        present = bool(frames[0]["present"])
        start = 0 if present else None
        for frame in frames[1:]:
            current = bool(frame["present"])
            when = min(duration_ms, int(frame["time_ms"]))
            if present and not current and start is not None and start < when:
                intervals.append([start, when])
                start = None
            elif not present and current:
                start = when
            present = current
        if present and start is not None and start < duration_ms:
            intervals.append([start, duration_ms])
        return intervals

    def _finish_recording(self, reason: str) -> dict[str, Any]:
        assert self._recording is not None
        elapsed = self._recording_time()
        tick_ms = 200
        duration_ms = max(tick_ms, math.ceil((elapsed + 1) / tick_ms) * tick_ms)
        nodes = []
        for role in sorted(self._recording["frames"]):
            frames = copy.deepcopy(self._recording["frames"][role])
            positions = [frames[0]]
            for frame in frames[1:]:
                if frame["position"] != positions[-1]["position"]:
                    positions.append(frame)
            node: dict[str, Any] = {"role": role}
            if "tx_gain_db_by_band" in self._nodes[role]:
                node["tx_gain_db_by_band"] = copy.deepcopy(self._nodes[role]["tx_gain_db_by_band"])
            if len(positions) == 1:
                node["position"] = positions[0]["position"]
            else:
                node["path"] = [
                    {"time_ms": int(frame["time_ms"]),
                     "position": frame["position"]}
                    for frame in positions
                ]
            presence = self._presence_intervals(frames, duration_ms)
            if presence != [[0, duration_ms]]:
                node["presence"] = presence
            nodes.append(node)
        mobility = {
            "schema": "wmdcfg.mobility.v1",
            "name": self._recording["name"],
            "tags": ["interactive-recording", f"clients-{len(set(self._allowed_roles) & self._selected_roles)}"],
            "duration_ms": duration_ms,
            "tick_ms": tick_ms,
            "seed": 0,
            "nodes": nodes,
        }
        recording_layout = copy.deepcopy(self.layout)
        recording_layout["nodes"] = [node for node in recording_layout["nodes"] if node["role"] in self._selected_roles]
        mobility["nodes"] = [node for node in mobility["nodes"] if node["role"] in self._selected_roles]
        world = compile_world(recording_layout, mobility)
        world["rf_nodes"] = {
            role: {key: copy.deepcopy(value) for key, value in self._nodes[role].items()
                   if key == "tx_gain_db_by_band"}
            for role in world["roles"]
        }
        world.pop("golden_sha256", None)
        world["golden_sha256"] = _hash(world)
        self._recorded_mobility = mobility
        self._recorded_world = world
        name = self._recording["name"]
        keyframes = sum(len(value) for value in self._recording["frames"].values())
        self._recording = None
        payload = {
            "revision": self._revision,
            "recording": {
                "active": False,
                "export_ready": True,
                "name": name,
                "duration_ms": duration_ms,
                "keyframes": keyframes,
                "reason": reason,
                "world_url": "/api/demo/recording/world",
            },
        }
        self.store.emit(
            "interaction.recording.stopped", self._world_time(), payload,
            producer="interaction",
        )
        return payload

    def stop_recording(
        self, *, token: str, expected_revision: Any
    ) -> dict[str, Any]:
        with self._lock:
            self._require_lease(token)
            self._validate_control_revision(expected_revision)
            if self._recording is None:
                raise InteractionError(409, "recording_inactive", "no recording is active")
            return self._finish_recording("operator_stopped")

    def recorded_world(self) -> dict[str, Any]:
        with self._lock:
            if self._recorded_world is None:
                raise InteractionError(409, "recording_unavailable", "stop a recording first")
            return copy.deepcopy(self._recorded_world)

    def recorded_documents(
        self,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        with self._lock:
            return (
                copy.deepcopy(self._recorded_mobility),
                copy.deepcopy(self._recorded_world),
            )

    def move(
        self,
        role: str,
        *,
        token: str,
        expected_revision: Any,
        destination: Any,
        speed_mps: Any,
        client_sequence: Any = None,
    ) -> dict[str, Any]:
        """Start a server-owned constant-speed path for one movable role."""
        with self._lock:
            self._validate_mutation(
                role, token, expected_revision, allowed_roles=self._movable_roles
            )
            target = self._clamp_position(destination)
            try:
                speed = float(speed_mps)
            except (TypeError, ValueError) as error:
                raise InteractionError(400, "invalid_speed", "speed_mps must be a number") from error
            if not math.isfinite(speed) or not 0.1 <= speed <= 10.0:
                raise InteractionError(
                    400, "invalid_speed", "speed_mps must be between 0.1 and 10.0",
                )
            self._cancel_role_movement(role, "superseded")
            start = list(self._roles[role]["position"])
            distance = math.dist(start, target)
            movement_id = secrets.token_hex(8)
            now = time.monotonic()
            movement = {
                "id": movement_id,
                "role": role,
                "status": "running",
                "start": start,
                "destination": target,
                "position": start,
                "speed_mps": round(speed, 3),
                "distance_m": round(distance, 3),
                "remaining_m": round(distance, 3),
                "duration_ms": round(distance / speed * 1000),
                "progress": 0.0,
                "client_sequence": client_sequence,
                "lease_token": token,
                "created_monotonic": now,
                "started_monotonic": now,
                "paused_monotonic": None,
                "paused_seconds": 0.0,
                "wake": threading.Event(),
            }
            self._movements[movement_id] = movement
            self._playback_overrides.add(role)
            self._revision += 1
            payload = {
                "revision": self._revision,
                "movement": self._public_movement(movement),
            }
            self.store.emit(
                "interaction.movement.started", self._world_time(), payload,
                producer="interaction",
            )
            thread = threading.Thread(
                target=self._movement_worker,
                args=(movement_id,),
                name=f"room-demo-move-{role}",
                daemon=True,
            )
            self._movement_threads[movement_id] = thread
            thread.start()
            return payload

    def _movement_tick(
        self, movement_id: str
    ) -> tuple[threading.Event | None, float, bool]:
        with self._lock:
            movement = self._movements[movement_id]
            self._expire_lease()
            if self._closing or movement["status"] in {"cancelled", "completed", "failed"}:
                return None, 0, True
            if movement["status"] == "paused":
                return movement["wake"], 0.5, False
            if (
                self._lease is None
                or not secrets.compare_digest(
                    movement["lease_token"], self._lease["token"]
                )
            ):
                self._cancel_movement(movement, "lease_lost")
                return None, 0, True

            now = time.monotonic()
            elapsed = max(
                0.0,
                now - movement["started_monotonic"] - movement["paused_seconds"],
            )
            duration = max(0.001, movement["duration_ms"] / 1000)
            fraction = min(1.0, elapsed / duration)
            point = self._clamp_position([
                movement["start"][axis]
                + (movement["destination"][axis] - movement["start"][axis]) * fraction
                for axis in (0, 1)
            ])
            previous = copy.deepcopy(self._roles[movement["role"]])
            self._roles[movement["role"]]["position"] = point
            try:
                applied = self._apply_role(
                    movement["role"], "position",
                    client_sequence=movement["client_sequence"],
                )
            except Exception as error:
                if self._faulted is None or self._faulted.startswith(
                    "unexpected external medium change:"
                ):
                    self._roles[movement["role"]] = previous
                movement["status"] = "failed"
                movement["reason"] = str(error)
                self.store.emit(
                    "interaction.movement.failed", self._world_time(),
                    {"revision": self._revision, "reason": str(error),
                     "movement": self._public_movement(movement)},
                    producer="interaction",
                )
                return None, 0, True
            movement["position"] = point
            movement["progress"] = round(fraction, 4)
            movement["remaining_m"] = round(
                math.dist(point, movement["destination"]), 3
            )
            movement["daemon_generation"] = applied["daemon_generation"]
            movement["revision"] = self._revision
            if fraction >= 1:
                movement["status"] = "completed"
                movement["remaining_m"] = 0.0
                kind = "interaction.movement.completed"
            else:
                kind = "interaction.movement.progress"
            self.store.emit(
                kind, self._world_time(),
                {"revision": self._revision,
                 "movement": self._public_movement(movement)},
                producer="interaction",
            )
            return (
                None if fraction >= 1 else movement["wake"],
                max(0.2, self.minimum_update_interval),
                fraction >= 1,
            )

    def _movement_worker(self, movement_id: str) -> None:
        while not self._closing:
            execute = self._command_executor
            try:
                if execute is None:
                    wake, delay, done = self._movement_tick(movement_id)
                else:
                    wake, delay, done = execute(self._movement_tick, movement_id)
            except RuntimeError as error:
                # RoomEngine withdraws command admission before it enqueues
                # the terminal restore. A movement clock racing that boundary
                # simply exits; it must not enqueue behind shutdown.
                if "room engine is closing" in str(error):
                    return
                raise
            if done or wake is None:
                return
            wake.wait(delay)
            wake.clear()

    def movement_control(
        self,
        movement_id: str,
        action: str,
        *,
        token: str,
        expected_revision: Any,
    ) -> dict[str, Any]:
        with self._lock:
            self._require_lease(token)
            try:
                revision = int(expected_revision)
            except (TypeError, ValueError) as error:
                raise InteractionError(
                    400, "invalid_revision", "expected_revision is required"
                ) from error
            # Progress advances the global room revision up to five times per
            # second. A movement-specific control remains unambiguous because
            # both its opaque ID and originating lease must match, so accept a
            # revision that was current when the operator clicked. A future
            # revision is never valid.
            if revision > self._revision:
                raise InteractionError(
                    409, "stale_revision",
                    f"expected revision {revision} is ahead of current revision "
                    f"{self._revision}",
                )
            movement = self._movements.get(movement_id)
            if movement is None:
                raise InteractionError(404, "movement_not_found", "movement does not exist")
            if not secrets.compare_digest(movement["lease_token"], token):
                raise InteractionError(403, "lease_mismatch", "movement belongs to another lease")
            now = time.monotonic()
            if action == "pause":
                if movement["status"] != "running":
                    raise InteractionError(409, "movement_not_running", "movement is not running")
                movement["status"] = "paused"
                movement["paused_monotonic"] = now
            elif action == "resume":
                if movement["status"] != "paused":
                    raise InteractionError(409, "movement_not_paused", "movement is not paused")
                movement["paused_seconds"] += now - movement["paused_monotonic"]
                movement["paused_monotonic"] = None
                movement["status"] = "running"
                movement["wake"].set()
            elif action == "cancel":
                if movement["status"] not in {"running", "paused"}:
                    raise InteractionError(409, "movement_not_active", "movement is not active")
                self._revision += 1
                movement["revision"] = self._revision
                self._cancel_movement(movement, "operator_cancelled")
                return {
                    "revision": self._revision,
                    "movement": self._public_movement(movement),
                }
            else:
                raise InteractionError(400, "invalid_action", "unknown movement action")
            self._revision += 1
            movement["revision"] = self._revision
            payload = {
                "revision": self._revision,
                "movement": self._public_movement(movement),
            }
            if action != "cancel":
                self.store.emit(
                    f"interaction.movement.{action}d", self._world_time(), payload,
                    producer="interaction",
                )
            return payload

    def _station_links(
        self, role: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        positions = {
            name: tuple(value["position"]) for name, value in self._roles.items()
        }
        present = {name: bool(value["present"]) for name, value in self._roles.items()}
        station = self._nodes[role]
        station_binding = self.plan["bindings"][role]
        updates: list[dict[str, Any]] = []
        summary: list[dict[str, Any]] = []
        for ap_role in sorted(
            name for name, kind in self.world["roles"].items()
            if kind == "fronthaul_ap"
        ):
            ap = self._nodes[ap_role]
            ap_binding = self.plan["bindings"][ap_role]
            down = directed_link(
                ap, station, positions, present, self.layout, {"seed": 0},
                self._revision + 1, "fronthaul",
            )
            up = directed_link(
                station, ap, positions, present, self.layout, {"seed": 0},
                self._revision + 1, "fronthaul",
            )
            for band in ("2.4", "5", "6"):
                frequency = ap_binding.get("fronthaul_frequencies_mhz", {}).get(band)
                if frequency is None:
                    continue
                radio = ap_binding.get("band_radios", {}).get(band)
                ap_mac = str((radio or {}).get("tx_mac") or ap_binding["radio_tx_mac"])
                station_mac = str(station_binding["radio_tx_mac"])
                for source, destination, value in (
                    (ap_mac, station_mac, down["snr_db_by_band"][band]),
                    (station_mac, ap_mac, up["snr_db_by_band"][band]),
                ):
                    updates.append({
                        "source": source,
                        "destination": destination,
                        "frequency_mhz": int(frequency),
                        "value": int(value),
                        "override": True,
                    })
                summary.append({
                    "ap_role": ap_role,
                    "band": band,
                    "frequency_mhz": int(frequency),
                    "distance_m": down["distance_m"],
                    "wall_loss_db": down["wall_loss_db"],
                    "snr_db": int(down["snr_db_by_band"][band]),
                })
        if not updates:
            raise InteractionError(500, "no_links", f"role {role!r} resolved no live RF links")
        if len(updates) > 30:
            raise InteractionError(
                422,
                "client_delta_too_large",
                f"role {role!r} resolved {len(updates)} RF keys; client limit is 30",
            )
        return updates, summary

    def _extender_links(
        self, role: str, *, include_backhaul: bool = False
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return every RF key affected by one mesh AP's room state.

        Presence changes leave mesh backhaul/control connectivity intact and
        model only loss/restoration of client-serving RF. Position changes
        also model mesh-peer links unless the fixed-pool world selector protects
        the startup mesh. Container identities and inventory remain unchanged.
        """
        positions = {
            name: tuple(value["position"]) for name, value in self._roles.items()
        }
        present = {
            name: bool(value["present"]) for name, value in self._roles.items()
        }
        ap = self._nodes[role]
        ap_binding = self.plan["bindings"][role]
        updates: list[dict[str, Any]] = []
        summary: list[dict[str, Any]] = []
        for station_role in self._allowed_roles:
            station = self._nodes[station_role]
            station_binding = self.plan["bindings"][station_role]
            down = directed_link(
                ap, station, positions, present, self.layout, {"seed": 0},
                self._revision + 1, "fronthaul",
            )
            up = directed_link(
                station, ap, positions, present, self.layout, {"seed": 0},
                self._revision + 1, "fronthaul",
            )
            for band in ("2.4", "5", "6"):
                frequency = ap_binding.get("fronthaul_frequencies_mhz", {}).get(band)
                if frequency is None:
                    continue
                radio = ap_binding.get("band_radios", {}).get(band)
                ap_mac = str((radio or {}).get("tx_mac") or ap_binding["radio_tx_mac"])
                station_mac = str(station_binding["radio_tx_mac"])
                for source, destination, value in (
                    (ap_mac, station_mac, down["snr_db_by_band"][band]),
                    (station_mac, ap_mac, up["snr_db_by_band"][band]),
                ):
                    updates.append({
                        "source": source,
                        "destination": destination,
                        "frequency_mhz": int(frequency),
                        "value": int(value),
                        "override": True,
                    })
                summary.append({
                    "link_class": "fronthaul",
                    "station_role": station_role,
                    "band": band,
                    "frequency_mhz": int(frequency),
                    "distance_m": down["distance_m"],
                    "wall_loss_db": down["wall_loss_db"],
                    "snr_db": int(down["snr_db_by_band"][band]),
                })
        if include_backhaul:
            # Fronthaul presence and mesh availability are independent demo
            # controls.  A disabled fronthaul may still be repositioned; its
            # live backhaul geometry therefore uses all provisioned mesh
            # nodes as present while client-serving links remain at minimum
            # SNR until the fronthaul is restored.
            backhaul_present = dict(present)
            ap_roles = sorted(
                name for name, kind in self.world["roles"].items()
                if kind == "fronthaul_ap"
            )
            for ap_role in ap_roles:
                backhaul_present[ap_role] = True
            for peer_role in ap_roles:
                if peer_role == role:
                    continue
                peer = self._nodes[peer_role]
                peer_binding = self.plan["bindings"][peer_role]
                outgoing = directed_link(
                    ap, peer, positions, backhaul_present, self.layout,
                    {"seed": 0}, self._revision + 1, "backhaul",
                )
                incoming = directed_link(
                    peer, ap, positions, backhaul_present, self.layout,
                    {"seed": 0}, self._revision + 1, "backhaul",
                )
                for band in ("2.4", "5", "6"):
                    radio = ap_binding.get("band_radios", {}).get(band)
                    peer_radio = peer_binding.get("band_radios", {}).get(band)
                    source_frequency = ap_binding.get(
                        "fronthaul_frequencies_mhz", {}
                    ).get(band)
                    peer_frequency = peer_binding.get(
                        "fronthaul_frequencies_mhz", {}
                    ).get(band)
                    if (
                        radio is None or peer_radio is None
                        or source_frequency is None or peer_frequency is None
                    ):
                        continue
                    source_mac = str(
                        radio.get("tx_mac") or ap_binding["radio_tx_mac"]
                    )
                    peer_mac = str(
                        peer_radio.get("tx_mac") or peer_binding["radio_tx_mac"]
                    )
                    updates.extend((
                        {
                            "source": source_mac,
                            "destination": peer_mac,
                            "frequency_mhz": int(source_frequency),
                            "value": int(outgoing["snr_db_by_band"][band]),
                            "override": True,
                        },
                        {
                            "source": peer_mac,
                            "destination": source_mac,
                            "frequency_mhz": int(peer_frequency),
                            "value": int(incoming["snr_db_by_band"][band]),
                            "override": True,
                        },
                    ))
                    summary.append({
                        "link_class": "backhaul",
                        "peer_role": peer_role,
                        "band": band,
                        "source_frequency_mhz": int(source_frequency),
                        "peer_frequency_mhz": int(peer_frequency),
                        "distance_m": outgoing["distance_m"],
                        "wall_loss_db": outgoing["wall_loss_db"],
                        "snr_db": int(outgoing["snr_db_by_band"][band]),
                    })
        expected_maximum = len(self._allowed_roles) * 3 * 2
        if include_backhaul:
            expected_maximum += (
                sum(
                    kind == "fronthaul_ap"
                    for kind in self.world["roles"].values()
                ) - 1
            ) * 3 * 2
        if not updates:
            raise InteractionError(
                500, "no_links", f"mesh AP {role!r} resolved no live RF links"
            )
        if len(updates) > expected_maximum:
            raise InteractionError(
                422,
                "extender_delta_too_large",
                f"role {role!r} resolved {len(updates)} RF keys; "
                f"extender movement limit is {expected_maximum}",
            )
        for item in updates:
            protected = self._protected_backhaul.get((item["source"], item["destination"], item["frequency_mhz"]))
            if protected is not None:
                item["value"], item["override"] = protected
        return updates, summary

    def _links_for_role(
        self, role: str, *, change: str = "position"
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if role in self._allowed_roles:
            return self._station_links(role)
        if (
            role in self._movable_roles
            and self.world["roles"][role] == "fronthaul_ap"
        ):
            return self._extender_links(
                role, include_backhaul=(change == "position")
            )
        raise InteractionError(
            400, "role_not_interactive", f"role {role!r} is not interactive"
        )

    def _apply_generation(self, updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Apply exactly the expected next generation under exclusive ownership."""
        assert self._client is not None
        if len(updates) > self._max_updates:
            raise InteractionError(
                422,
                "medium_update_limit",
                f"mutation requires {len(updates)} RF keys; daemon limit is "
                f"{self._max_updates}",
            )
        status = self._client.status()
        if (
            status.instance_id != self._instance_id
            or status.generation != self._generation
        ):
            reason = (
                "unexpected external medium change: "
                f"expected instance={self._instance_id} generation={self._generation}, "
                f"observed instance={status.instance_id} generation={status.generation}"
            )
            self._faulted = reason
            if self.recovery is not None:
                self.recovery.failed(reason, contaminated=True)
            self.store.emit(
                "medium.external_write_detected",
                self._world_time(),
                {
                    "expected_instance_id": self._instance_id,
                    "observed_instance_id": status.instance_id,
                    "expected_generation": self._generation,
                    "observed_generation": status.generation,
                },
                producer="interaction",
            )
            raise ActuatorError(reason)
        generation = self._generation + 1
        if self.recovery is not None:
            self.recovery.before_apply(self._generation, generation)
        try:
            applied = self._client.apply_frequency(generation, updates)
        except Exception as error:
            if self.recovery is not None:
                self.recovery.failed(error)
            raise
        self._generation = generation
        if self.recovery is not None:
            self.recovery.committed(generation)
        return applied

    def backhaul_policy(self) -> str:
        return "adaptive-rdk" if self.adaptive_backhaul else (
            "fixed-startup-mesh" if self.worlds is not None and not self.model_backhaul else "modeled")

    def backhaul_links(self) -> list[dict[str, Any]]:
        """Expose verified applied RF, not a fresh controller SNR measurement."""
        links = []
        bindings = {role: binding for role, binding in self.plan["bindings"].items()
                    if binding["role_type"] == "fronthaul_ap"}
        if self._client is None or self._faulted or self._restored:
            return links
        for source_role, source in bindings.items():
            for destination_role, destination in bindings.items():
                if source_role == destination_role:
                    continue
                for band, radio in source.get("band_radios", {}).items():
                    peer = destination.get("band_radios", {}).get(band)
                    frequency = source.get("fronthaul_frequencies_mhz", {}).get(band)
                    if peer is None or frequency is None:
                        continue
                    value = self._applied_values.get((radio["tx_mac"], peer["tx_mac"], int(frequency)))
                    if value is not None and value[1]:
                        links.append({"source_role": source_role, "destination_role": destination_role,
                                      "band": band, "frequency_mhz": int(frequency), "snr_db": value[0],
                                      "source": "wmediumd_verified_applied_rf"})
        return links

    def backhaul_action(self, action: Callable[[], Any], *, expected_epoch: int) -> Any:
        """Serialize native parent selection with RF edits and client BTM."""
        with self._lock:
            if not self.adaptive_backhaul or self._client is None or self._faulted or self._closing:
                raise InteractionError(409, "backhaul_unavailable", "adaptive backhaul is unavailable")
            if expected_epoch != self._environment_epoch:
                raise InteractionError(409, "environment_changed", "room changed before backhaul selection")
            started = time.monotonic()
            try:
                return action()
            finally:
                if self._lease is not None:
                    elapsed = time.monotonic() - started
                    self._lease["expires_monotonic"] += elapsed
                    self._lease["expires_at"] = (
                        dt.datetime.fromisoformat(self._lease["expires_at"]) + dt.timedelta(seconds=elapsed)
                    ).isoformat()
                self._mark_rf_committed()

    def steering_action(
        self,
        station_role: str,
        source_ap_role: str,
        target_ap_role: str,
        band: str,
        action: Callable[[], Any],
        *,
        expected_epoch: int | None = None,
    ) -> Any:
        """Run one BTM action under a temporary, exactly-restored RF assist.

        The optimizer has already selected the target from controller telemetry
        before this method is entered.  This transaction only makes that one
        nominated target unambiguous to hwsim's client-side scan/roam logic.
        RoomEngine serialization prevents an interactive move from racing the
        temporary generation, and the authoritative room matrix is restored
        and read back before this method returns.
        """
        with self._lock:
            if self._client is None:
                raise InteractionError(
                    503, "not_started", "interactive medium is unavailable"
                )
            if expected_epoch is not None and expected_epoch != self._environment_epoch:
                raise InteractionError(409, "environment_changed", "world changed after the steering observation")
            if self._faulted is not None:
                raise InteractionError(
                    503, "session_faulted", self._faulted
                )
            if station_role not in self._allowed_roles:
                raise InteractionError(
                    400, "role_not_interactive",
                    f"role {station_role!r} is not an interactive station",
                )
            if not self._roles[station_role]["present"] or not self._roles.get(target_ap_role, {}).get("present"):
                raise InteractionError(409, "role_offline", "cannot steer an offline client or to disabled fronthaul")
            if source_ap_role == target_ap_role:
                raise InteractionError(
                    409, "same_steering_ap", "source and target AP are identical"
                )
            ap_roles = sorted(
                role for role, kind in self.world["roles"].items()
                if kind == "fronthaul_ap"
            )
            if source_ap_role not in ap_roles or target_ap_role not in ap_roles:
                raise InteractionError(
                    400, "unknown_steering_ap", "source or target AP is not in the room"
                )
            target_binding = self.plan["bindings"][target_ap_role]
            frequency = target_binding.get("fronthaul_frequencies_mhz", {}).get(band)
            if frequency is None:
                raise InteractionError(
                    400, "unsupported_steering_band",
                    f"target AP has no {band} GHz fronthaul",
                )
            frequency = int(frequency)
            station_mac = str(
                self.plan["bindings"][station_role]["radio_tx_mac"]
            )
            prior: list[dict[str, Any]] = []
            temporary: list[dict[str, Any]] = []
            for ap_role in ap_roles:
                binding = self.plan["bindings"][ap_role]
                if int(binding.get("fronthaul_frequencies_mhz", {}).get(band, -1)) != frequency:
                    continue
                radio = binding.get("band_radios", {}).get(band)
                ap_mac = str((radio or {}).get("tx_mac") or binding["radio_tx_mac"])
                desired = (
                    60 if ap_role == target_ap_role
                    else 20 if ap_role == source_ap_role
                    else -20
                )
                for source, destination in (
                    (ap_mac, station_mac), (station_mac, ap_mac)
                ):
                    key = (source, destination, frequency)
                    if key not in self._applied_values:
                        raise InteractionError(
                            500, "steering_link_missing",
                            f"room has no applied link {source} -> {destination} "
                            f"at {frequency} MHz",
                        )
                    value, overridden = self._applied_values[key]
                    prior.append({
                        "source": source,
                        "destination": destination,
                        "frequency_mhz": frequency,
                        "value": value,
                        "override": overridden,
                    })
                    if (value, overridden) != (desired, True):
                        temporary.append({
                            "source": source,
                            "destination": destination,
                            "frequency_mhz": frequency,
                            "value": desired,
                            "override": True,
                        })

            self.store.emit(
                "rf.steering_assist.started", self._world_time(),
                {
                    "station_role": station_role,
                    "source_ap_role": source_ap_role,
                    "target_ap_role": target_ap_role,
                    "band": band,
                    "frequency_mhz": frequency,
                    "source_snr_db": 20,
                    "target_snr_db": 60,
                    "other_snr_db": -20,
                    "changed_link_count": len(temporary),
                },
                producer="room-engine",
            )
            result: Any = None
            action_error: BaseException | None = None
            temporary_active = False
            try:
                if temporary:
                    applied = self._apply_generation(temporary)
                    temporary_active = True
                    for item in applied:
                        key = (
                            item["source"], item["destination"],
                            item["frequency_mhz"],
                        )
                        _, value, overridden = self._client.get_frequency_link(*key)
                        if (value, overridden) != (item["value"], item["override"]):
                            raise ActuatorError(
                                "steering assist generation readback mismatch"
                            )
                        self._applied_values[key] = (value, overridden)
                result = action()
            except BaseException as error:
                action_error = error
            finally:
                if temporary_active:
                    restored = self._apply_generation(prior)
                    for item in restored:
                        key = (
                            item["source"], item["destination"],
                            item["frequency_mhz"],
                        )
                        _, value, overridden = self._client.get_frequency_link(*key)
                        if (value, overridden) != (item["value"], item["override"]):
                            self._faulted = (
                                "steering assist failed to restore the room RF matrix"
                            )
                            if self.recovery is not None:
                                self.recovery.failed(self._faulted)
                            raise ActuatorError(self._faulted)
                        self._applied_values[key] = (value, overridden)
                    # Measurements collected while the temporary steering
                    # matrix was active are not room truth.  Mark a freshness
                    # boundary without changing the environment epoch: the
                    # authoritative geometry is unchanged and optimizer
                    # cooldown/history must survive this transaction.
                    self._mark_measurements_stale(station_role)
                self.store.emit(
                    "rf.steering_assist.completed", self._world_time(),
                    {
                        "station_role": station_role,
                        "source_ap_role": source_ap_role,
                        "target_ap_role": target_ap_role,
                        "band": band,
                        "frequency_mhz": frequency,
                        "room_matrix_restored": True,
                        "action_success": (
                            None if result is None else
                            bool(getattr(result, "success", True))
                        ),
                        "error": None if action_error is None else str(action_error),
                        "daemon_generation": self._generation,
                        "environment_epoch": self._environment_epoch,
                        "measurement_epoch": self._measurement_epoch,
                    },
                    producer="room-engine",
                )
            if action_error is not None:
                raise action_error
            return result

    def _mark_rf_committed(self, role: str | None = None) -> None:
        self._environment_epoch += 1
        if role is None:
            self._recent_rf_roles.clear()
        else:
            self._recent_rf_roles[role] = time.monotonic()
        if role is None or self.world["roles"].get(role) == "fronthaul_ap":
            self._backhaul_epoch += 1
            self._last_backhaul_change_monotonic = time.monotonic()
        self._mark_measurements_stale(role)

    def _mark_measurements_stale(self, role: str | None = None) -> None:
        self._measurement_epoch += 1
        self._last_rf_apply_monotonic = time.monotonic()
        self._last_rf_applied_at = dt.datetime.now(dt.timezone.utc).isoformat()
        self._last_rf_role = role

    def _capture_baseline(self, updates: list[dict[str, Any]]) -> None:
        assert self._client is not None
        for item in updates:
            key = (item["source"], item["destination"], item["frequency_mhz"])
            if key in self._baseline:
                continue
            _, value, overridden = self._client.get_frequency_link(*key)
            self._baseline[key] = (value, overridden)

    def _apply_role(
        self, role: str, change: str, *, client_sequence: Any
    ) -> dict[str, Any]:
        return self._apply_roles([(role, change)], client_sequence=client_sequence)[0]

    def _apply_roles(self, changes, *, client_sequence, publish_roles=True):
        assert self._client is not None
        unique_updates = {}
        links_by_change = {}
        for role, change in changes:
            updates, links = self._links_for_role(role, change=change)
            links_by_change[(role, change)] = links
            for item in updates:
                unique_updates[(item["source"], item["destination"], item["frequency_mhz"])] = item
        updates = list(unique_updates.values())
        changed = [
            item
            for item in updates
            if self._applied_values.get(
                (item["source"], item["destination"], item["frequency_mhz"])
            ) != (item["value"], item["override"])
        ]
        applied_started = False
        try:
            self._capture_baseline(updates)
            if changed or any(change == "presence" for _role, change in changes):
                with parallel_disconnections(
                    self.disconnect_client(role) for role, change in changes
                    if self.disconnect_client is not None and change == "presence"
                    and role in self._allowed_roles and not self._roles[role]["present"]
                ):
                    applied = self._apply_generation(changed) if changed else []
                    applied_started = True
                readback = []
                for item in applied:
                    _, value, overridden = self._client.get_frequency_link(
                        item["source"], item["destination"], item["frequency_mhz"]
                    )
                    readback.append({**item, "value": value, "override": overridden})
                if readback != applied:
                    raise ActuatorError("interactive generation readback mismatch")
                for item in applied:
                    key = (item["source"], item["destination"], item["frequency_mhz"])
                    self._applied_values[key] = (item["value"], item["override"])
                roles = {role for role, _change in changes}
                parallel_reconnections(self.reconnect_client, (
                    role for role, change in changes
                    if self.reconnect_client is not None and change == "presence"
                    and role in self._allowed_roles and self._roles[role]["present"]
                ))
                representative = min(roles, key=lambda role: (self.world["roles"].get(role) != "fronthaul_ap", role))
                self._mark_rf_committed(representative)
                self._recent_rf_roles.update({role: time.monotonic() for role in roles})
            else:
                applied = []
            self._revision += 1
            for role in sorted({role for role, _change in changes}):
                self._record_frame(role)
        except Exception as error:
            if applied_started:
                self._faulted = str(error)
                self._roles = copy.deepcopy(self._initial_roles)
                self.restore()
            raise
        payloads = []
        for role, change in changes:
            payload = {
                "revision": self._revision,
                "role": role,
                "position": list(self._roles[role]["position"]),
                "present": self._roles[role]["present"],
                "change": change,
                "client_sequence": client_sequence,
                "daemon_generation": self._generation,
                "environment_epoch": self._environment_epoch,
                "changed_link_count": len(applied),
                "links": links_by_change[(role, change)],
            }
            if publish_roles:
                self.store.emit(
                    f"room.{change}.committed", self._world_time(), payload,
                    producer="interaction",
                )
            payloads.append(payload)
        self.store.emit(
            "rf.generation.applied" if applied else "rf.generation.noop",
            self._world_time(),
            {
                "revision": self._revision,
                "role": changes[0][0] if len(changes) == 1 else None,
                "roles": sorted({role for role, _change in changes}),
                "cause": changes[0][1] if len(changes) == 1 else "playback_batch",
                "daemon_instance_id": self._instance_id,
                "daemon_generation": self._generation,
                "environment_epoch": self._environment_epoch,
                "changed_link_count": len(applied),
            },
            producer="interaction",
        )
        return payloads

    def restore(self) -> bool:
        with self._lock:
            if self._client is None:
                return self._restored
            self.store.emit(
                "rf.restore.started", self._world_time(),
                {"touched_links": len(self._baseline)}, producer="interaction",
            )
            if self.recovery is not None:
                self.recovery.restoring()
            if self._faulted and self._faulted.startswith(
                "unexpected external medium change:"
            ):
                self._restored = False
                self.store.emit(
                    "rf.restore.completed",
                    self._world_time(),
                    {
                        "verified": False,
                        "reason": "medium ownership was lost; automatic restore suppressed",
                        "daemon_generation": self._generation,
                        "restored_links": 0,
                    },
                    producer="interaction",
                )
                if self.recovery is not None:
                    self.recovery.failed(self._faulted, contaminated=True)
                return False
            restored = True
            if self._baseline:
                updates = [
                    {
                        "source": source,
                        "destination": destination,
                        "frequency_mhz": frequency,
                        "value": value if overridden else 0,
                        "override": overridden,
                    }
                    for (source, destination, frequency), (value, overridden)
                    in sorted(self._baseline.items())
                ]
                self._apply_generation(updates)
                for item in updates:
                    key = (item["source"], item["destination"], item["frequency_mhz"])
                    _, value, overridden = self._client.get_frequency_link(*key)
                    expected_value, expected_override = self._baseline[key]
                    if value != expected_value or overridden != expected_override:
                        restored = False
            self._restored = restored
            if self.recovery is not None:
                self.recovery.completed(self._generation, restored)
                if restored:
                    self.recovery.resume_clients()
            self.store.emit(
                "rf.restore.completed", self._world_time(),
                {
                    "verified": restored,
                    "daemon_generation": self._generation,
                    "restored_links": len(self._baseline),
                },
                producer="interaction",
            )
            return restored

    def close(self) -> bool:
        with self._lock:
            self._closing = True
            self._pause_playback("session_stopped")
            for movement in self._movements.values():
                self._cancel_movement(movement, "session_stopped")
            if self._recording is not None:
                self._finish_recording("session_stopped")
            threads = list(self._movement_threads.values())
            if self._playback_thread is not None:
                threads.append(self._playback_thread)
        for thread in threads:
            thread.join(timeout=2)
        with self._lock:
            if self._client is None:
                return self._restored
            try:
                return self.restore()
            finally:
                self._client.close()
                self._client = None
