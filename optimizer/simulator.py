from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any

from .experiments import ExperimentError
from .model import (
    CandidateObservation,
    ClientObservation,
    MeshHealth,
    Snapshot,
    format_time,
)
from .policy import Evaluation, ThresholdPolicy
from .state import PolicyState


BANDS = ("2.4", "5", "6")
_BAND_OCTET = {"2.4": 24, "5": 50, "6": 60}
_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _normalize_numbers(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_normalize_numbers(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_numbers(item) for key, item in value.items()}
    return value


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            _normalize_numbers(value),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _mac(prefix: tuple[int, int, int], index: int, suffix: int) -> str:
    if not 0 <= index <= 0xFFFF:
        raise ExperimentError("synthetic identity index exceeds 16 bits")
    values = (*prefix, (index >> 8) & 0xFF, index & 0xFF, suffix)
    return ":".join(f"{value:02x}" for value in values)


def _identity(world: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    agents: dict[str, dict[str, Any]] = {}
    clients: dict[str, str] = {}
    agent_roles = sorted(role for role, kind in world["roles"].items() if kind == "fronthaul_ap")
    station_roles = sorted(role for role, kind in world["roles"].items() if kind == "station")
    for index, role in enumerate(agent_roles, 1):
        agents[role] = {
            "device_id": _mac((0x02, 0x10, 0x00), index, 0x20),
            "name": "Gateway" if role == "gateway" else role.replace("_", "-").title(),
            "bsses": {
                band: _mac((0x02, 0x11, _BAND_OCTET[band]), index, 0x01)
                for band in BANDS
            },
        }
    for index, role in enumerate(station_roles, 1):
        clients[role] = _mac((0x02, 0x20, 0x00), index, 0x00)
    return agents, clients


def _rcpi(snr_db: int, noise_floor_dbm: int) -> int:
    # RCPI = 2 * (RSSI dBm + 110), with RSSI synthesized from SNR plus the
    # configured receiver noise floor. This is a test sensor model, not a
    # claim that a physical receiver has a fixed noise floor.
    return max(0, min(220, 2 * (int(snr_db) + int(noise_floor_dbm) + 110)))


@dataclass(frozen=True)
class SimulationConfig:
    initial_band: str = "5"
    initial_association_uptime_seconds: int = 120
    metric_delay_ms: int = 0
    minimum_observable_snr_db: int = -10
    noise_floor_dbm_24: int = -95
    noise_floor_dbm_5: int = -95
    noise_floor_dbm_6: int = -95
    weak_rcpi_floor: int = 100

    def __post_init__(self) -> None:
        if self.initial_band not in BANDS:
            raise ValueError(f"initial_band must be one of {BANDS}")
        if self.initial_association_uptime_seconds < 0:
            raise ValueError("initial association uptime cannot be negative")
        if self.metric_delay_ms < 0:
            raise ValueError("metric delay cannot be negative")
        if not -20 <= self.minimum_observable_snr_db <= 60:
            raise ValueError("minimum observable SNR must be in [-20, 60]")
        if not 0 <= self.weak_rcpi_floor <= 220:
            raise ValueError("weak RCPI floor must be in [0, 220]")

    def noise_floor(self, band: str) -> int:
        return {
            "2.4": self.noise_floor_dbm_24,
            "5": self.noise_floor_dbm_5,
            "6": self.noise_floor_dbm_6,
        }[band]


@dataclass
class _Association:
    agent_role: str
    band: str
    uptime_seconds: int


class WorldSimulator:
    """Closed-loop policy test double driven by a verified golden world.

    This class is evaluator infrastructure. It synthesizes EasyMesh-shaped
    telemetry and is never used by the live ControllerObserver.
    """

    def __init__(
        self,
        world: dict[str, Any],
        policy: ThresholdPolicy,
        *,
        config: SimulationConfig | None = None,
        client_behavior: dict[str, str] | None = None,
    ) -> None:
        if world.get("schema") != "wmdcfg.world-plan.v1":
            raise ExperimentError("simulation requires a wmdcfg.world-plan.v1")
        unsigned = dict(world)
        claimed = unsigned.pop("golden_sha256", None)
        if claimed != _hash(unsigned):
            raise ExperimentError("world golden_sha256 does not match its contents")
        self.world = world
        self.policy = policy
        self.config = config or SimulationConfig()
        self.behavior = dict(client_behavior or {})
        unknown = sorted(set(self.behavior) - set(world["roles"]))
        if unknown:
            raise ExperimentError(f"client behavior names unknown roles: {unknown}")
        invalid = sorted(set(self.behavior.values()) - {"accept", "reject", "ignore"})
        if invalid:
            raise ExperimentError(f"unsupported client behaviors: {invalid}")
        self.agents, self.clients = _identity(world)
        self.associations: dict[str, _Association] = {}
        self.state = PolicyState()

    def _link(self, generation: dict[str, Any], station: str, agent: str) -> dict[str, Any]:
        return next(
            item for item in generation["links"]
            if item["link_class"] == "fronthaul"
            and item["source_role"] == station
            and item["destination_role"] == agent
        )

    def _best_agent(self, generation: dict[str, Any], station: str, band: str) -> str:
        return min(
            self.agents,
            key=lambda role: (
                -int(self._link(generation, station, role)["snr_db_by_band"][band]),
                role,
            ),
        )

    def _ensure_associations(self, generation: dict[str, Any]) -> None:
        for role in self.clients:
            if role in self.associations or not generation["present"].get(role, False):
                continue
            self.associations[role] = _Association(
                self._best_agent(generation, role, self.config.initial_band),
                self.config.initial_band,
                self.config.initial_association_uptime_seconds,
            )

    def _snapshot(self, generation: dict[str, Any], sequence: int) -> Snapshot:
        self._ensure_associations(generation)
        observed = _START + timedelta(milliseconds=int(generation["time_ms"]))
        measured = observed - timedelta(milliseconds=self.config.metric_delay_ms)
        clients = []
        candidates = []
        for role, sta_mac in sorted(self.clients.items()):
            if not generation["present"].get(role, False):
                continue
            association = self.associations[role]
            agent = self.agents[association.agent_role]
            link = self._link(generation, role, association.agent_role)
            current_snr = int(link["snr_db_by_band"][association.band])
            clients.append(
                ClientObservation(
                    sta_mac=sta_mac,
                    connected_device_id=agent["device_id"],
                    connected_device_name=agent["name"],
                    connected_bssid=agent["bsses"][association.band],
                    rcpi=_rcpi(current_snr, self.config.noise_floor(association.band)),
                    association_uptime_seconds=association.uptime_seconds,
                    metric_observed_at=format_time(measured),
                    measurement_source="simulated_associated_sta_link_metrics",
                    band=association.band,
                )
            )
            for agent_role, candidate_agent in sorted(self.agents.items()):
                candidate_link = self._link(generation, role, agent_role)
                for band in BANDS:
                    bssid = candidate_agent["bsses"][band]
                    if bssid == agent["bsses"][association.band]:
                        continue
                    snr = int(candidate_link["snr_db_by_band"][band])
                    candidates.append(
                        CandidateObservation(
                            sta_mac=sta_mac,
                            bssid=bssid,
                            device_id=candidate_agent["device_id"],
                            device_name=candidate_agent["name"],
                            rcpi=(
                                _rcpi(snr, self.config.noise_floor(band))
                                if snr >= self.config.minimum_observable_snr_db else None
                            ),
                            metric_observed_at=(
                                format_time(measured)
                                if snr >= self.config.minimum_observable_snr_db else None
                            ),
                            measurement_source="simulated_unassociated_sta_link_metrics",
                            band=band,
                            eligible=generation["present"].get(agent_role, False),
                        )
                    )
        return Snapshot(
            schema_version=1,
            sequence=sequence,
            observed_at=format_time(observed),
            controller_url=f"simulated://{self.world['name']}",
            health=MeshHealth(
                devices=len(self.agents),
                clients=len(clients),
                radios=len(self.agents) * 3,
                bsses=len(self.agents) * 3,
                source="golden_world_sensor_model",
            ),
            clients=tuple(clients),
            candidates=tuple(candidates),
        )

    def _apply(self, evaluation: Evaluation) -> list[dict[str, Any]]:
        role_by_mac = {mac: role for role, mac in self.clients.items()}
        bss_index = {
            bssid: (role, band)
            for role, agent in self.agents.items()
            for band, bssid in agent["bsses"].items()
        }
        outcomes = []
        for decision in evaluation.decisions:
            if decision.action != "steer" or decision.target_bssid is None:
                continue
            role = role_by_mac[decision.sta_mac]
            behavior = self.behavior.get(role, "accept")
            outcome = behavior
            if behavior == "accept":
                target_role, target_band = bss_index[decision.target_bssid]
                self.associations[role] = _Association(target_role, target_band, 0)
            outcomes.append(
                {
                    "sta_role": role,
                    "sta_mac": decision.sta_mac,
                    "source_bssid": decision.source_bssid,
                    "target_bssid": decision.target_bssid,
                    "behavior": behavior,
                    "outcome": outcome,
                }
            )
        return outcomes

    def run(self) -> dict[str, Any]:
        cycles = []
        totals = {"attempts": 0, "accepted": 0, "rejected": 0, "ignored": 0}
        weak_station_ticks = 0
        tick_seconds = int(self.world["tick_ms"]) // 1000
        for sequence, generation in enumerate(self.world["generations"]):
            snapshot = self._snapshot(generation, sequence)
            evaluation = self.policy.evaluate(snapshot, self.state)
            self.state = evaluation.state
            outcomes = self._apply(evaluation)
            totals["attempts"] += len(outcomes)
            for item in outcomes:
                totals[
                    {"accept": "accepted", "reject": "rejected", "ignore": "ignored"}[
                        item["outcome"]
                    ]
                ] += 1
            weak_station_ticks += sum(
                client.rcpi is not None and client.rcpi < self.config.weak_rcpi_floor
                for client in snapshot.clients
            )
            cycles.append(
                {
                    "time_ms": generation["time_ms"],
                    "snapshot": snapshot.to_dict(),
                    "evaluation": evaluation.to_dict(),
                    "outcomes": outcomes,
                }
            )
            for association in self.associations.values():
                association.uptime_seconds += tick_seconds
        result = {
            "schema": "optimizer.world-simulation.v1",
            "world": self.world["name"],
            "world_golden_sha256": self.world["golden_sha256"],
            "policy_hash": self.policy.config.digest(),
            "truth_boundary": {
                "kind": "synthetic_test_telemetry",
                "live_observer_compatible": False,
                "production_optimizer_reads_world": False,
            },
            "summary": {
                **totals,
                "weak_station_ticks": weak_station_ticks,
                "cycles": len(cycles),
                "final_associations": {
                    role: {
                        "agent": association.agent_role,
                        "band": association.band,
                    }
                    for role, association in sorted(self.associations.items())
                },
            },
            "cycles": cycles,
        }
        unsigned = dict(result)
        result["simulation_sha256"] = _hash(unsigned)
        return result
