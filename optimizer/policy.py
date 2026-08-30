from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
import hashlib
import json
from typing import Any

from .model import CandidateObservation, ClientObservation, Snapshot, parse_time
from .state import ClientPolicyState, PolicyState


@dataclass(frozen=True)
class PolicyConfig:
    policy_version: int = 1
    decision_interval_seconds: float = 1
    current_rcpi_below: int = 100
    minimum_target_gain_rcpi: int = 16
    condition_hold_seconds: float = 5
    minimum_dwell_seconds: int = 20
    steer_timeout_seconds: float = 10
    post_steer_cooldown_seconds: float = 30
    failure_backoff_seconds: float = 60
    maximum_failure_backoff_seconds: float = 600
    reject_stale_metrics_after_seconds: float = 15
    band_upgrade_enabled: bool = False
    minimum_band_upgrade_target_rcpi: int = 120
    maximum_band_upgrade_loss_rcpi: int = 8
    expected_devices: int = 5
    expected_clients: int = 10

    def __post_init__(self) -> None:
        if self.policy_version != 1:
            raise ValueError("only policy_version 1 is supported")
        for name, value in asdict(self).items():
            if name == "policy_version":
                continue
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
        if not 0 <= self.current_rcpi_below <= 220:
            raise ValueError("current_rcpi_below must be a valid RCPI")
        if not 0 <= self.minimum_band_upgrade_target_rcpi <= 220:
            raise ValueError("minimum_band_upgrade_target_rcpi must be a valid RCPI")
        if self.maximum_band_upgrade_loss_rcpi > 220:
            raise ValueError("maximum_band_upgrade_loss_rcpi must not exceed 220")
        if self.maximum_failure_backoff_seconds < self.failure_backoff_seconds:
            raise ValueError(
                "maximum_failure_backoff_seconds must be at least failure_backoff_seconds"
            )

    def digest(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class CandidateScore:
    bssid: str
    rcpi: int
    gain_rcpi: int
    band: str | None = None
    band_rank_delta: int = 0


@dataclass(frozen=True)
class Decision:
    sta_mac: str
    action: str
    reason: str
    source_bssid: str
    target_bssid: str | None = None
    current_band: str | None = None
    target_band: str | None = None
    current_rcpi: int | None = None
    target_rcpi: int | None = None
    hold_seconds: float = 0
    scores: tuple[CandidateScore, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Evaluation:
    policy_hash: str
    decisions: tuple[Decision, ...]
    state: PolicyState

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_hash": self.policy_hash,
            "decisions": [item.to_dict() for item in self.decisions],
            "state": self.state.to_dict(),
        }


def _fresh(timestamp: str | None, now, maximum_age: float) -> bool:
    if timestamp is None:
        return False
    observed = parse_time(timestamp)
    age = (now - observed).total_seconds()
    return 0 <= age <= maximum_age


_BAND_RANK = {"2.4": 0, "5": 1, "6": 2}


class ThresholdPolicy:
    def __init__(self, config: PolicyConfig) -> None:
        self.config = config

    def evaluate(self, snapshot: Snapshot, prior: PolicyState | None = None) -> Evaluation:
        state = prior or PolicyState()
        decisions: list[Decision] = []
        now = parse_time(snapshot.observed_at)

        health_reason = None
        if snapshot.health.devices != self.config.expected_devices:
            health_reason = "mesh_device_count_mismatch"
        elif snapshot.health.clients != self.config.expected_clients:
            health_reason = "client_count_mismatch"

        for client in snapshot.clients:
            old = state.for_sta(client.sta_mac)
            decision, new = self._evaluate_client(
                snapshot, client, old, now, health_reason
            )
            decisions.append(decision)
            state = state.replace(new)
        return Evaluation(self.config.digest(), tuple(decisions), state)

    def _evaluate_client(
        self,
        snapshot: Snapshot,
        client: ClientObservation,
        old: ClientPolicyState,
        now,
        health_reason: str | None,
    ) -> tuple[Decision, ClientPolicyState]:
        base = dict(
            sta_mac=client.sta_mac,
            source_bssid=client.connected_bssid,
            current_rcpi=client.rcpi,
            current_band=client.band,
        )
        stable = ClientPolicyState(
            sta_mac=client.sta_mac,
            phase="stable",
            source_bssid=client.connected_bssid,
            failure_count=old.failure_count,
            last_failure_reason=old.last_failure_reason,
            last_action_at=old.last_action_at,
        )

        if old.phase == "pending" and old.target_bssid:
            if client.connected_bssid == old.target_bssid:
                until = now + timedelta(seconds=self.config.post_steer_cooldown_seconds)
                new = ClientPolicyState(
                    sta_mac=client.sta_mac,
                    phase="cooldown",
                    source_bssid=client.connected_bssid,
                    cooldown_until=until.isoformat(),
                    failure_count=0,
                    last_action_at=old.pending_since,
                )
                return Decision(action="none", reason="target_association_observed", **base), new
            if old.pending_since and (
                now - parse_time(old.pending_since)
            ).total_seconds() <= self.config.steer_timeout_seconds:
                return Decision(action="none", reason="steer_pending", **base), old
            failures = old.failure_count + 1
            backoff = min(
                self.config.maximum_failure_backoff_seconds,
                self.config.failure_backoff_seconds * (2 ** min(failures - 1, 30)),
            )
            until = now + timedelta(seconds=backoff)
            failed = ClientPolicyState(
                sta_mac=client.sta_mac,
                phase="backoff",
                source_bssid=client.connected_bssid,
                backoff_until=until.isoformat(),
                failure_count=failures,
                last_failure_reason="association_timeout",
                last_action_at=old.pending_since,
            )
            return Decision(action="none", reason="steer_timeout_backoff", **base), failed

        if old.phase == "backoff" and old.backoff_until:
            if now < parse_time(old.backoff_until):
                return Decision(action="none", reason="steer_failure_backoff", **base), old
            stable = ClientPolicyState(
                sta_mac=client.sta_mac,
                phase="stable",
                source_bssid=client.connected_bssid,
                failure_count=old.failure_count,
                last_failure_reason=old.last_failure_reason,
                last_action_at=old.last_action_at,
            )

        if old.phase == "cooldown" and old.cooldown_until:
            if now < parse_time(old.cooldown_until):
                return Decision(action="none", reason="post_steer_cooldown", **base), old

        if health_reason:
            return Decision(action="none", reason=health_reason, **base), stable
        if client.rcpi is None:
            return Decision(action="none", reason="current_metric_missing", **base), stable
        if not _fresh(
            client.metric_observed_at,
            now,
            self.config.reject_stale_metrics_after_seconds,
        ):
            reason = (
                "current_metric_freshness_unknown"
                if client.metric_observed_at is None
                else "current_metric_stale"
            )
            return Decision(action="none", reason=reason, **base), stable
        if client.association_uptime_seconds < self.config.minimum_dwell_seconds:
            return Decision(action="none", reason="minimum_dwell_not_met", **base), stable
        current_is_weak = client.rcpi < self.config.current_rcpi_below
        if not current_is_weak and not self.config.band_upgrade_enabled:
            return Decision(action="none", reason="current_link_acceptable", **base), stable

        observed_candidates = [
            item
            for item in snapshot.candidates_for(client.sta_mac)
            if item.eligible
            and item.bssid != client.connected_bssid
            and item.rcpi is not None
            and _fresh(
                item.metric_observed_at,
                now,
                self.config.reject_stale_metrics_after_seconds,
            )
        ]
        if not observed_candidates:
            reason = (
                "fresh_candidate_metric_missing"
                if current_is_weak else "fresh_band_candidate_metric_missing"
            )
            return Decision(action="none", reason=reason, **base), stable

        current_rank = _BAND_RANK.get(client.band)
        ranked = sorted(observed_candidates, key=lambda item: (-int(item.rcpi), item.bssid))
        scores = self._scores(ranked, client)
        band_upgrade = False
        if not current_is_weak:
            if current_rank is None:
                return Decision(action="none", reason="current_band_unknown", **base), stable
            upgrades = [
                item for item in observed_candidates
                if _BAND_RANK.get(item.band, -1) > current_rank
                and int(item.rcpi) >= self.config.minimum_band_upgrade_target_rcpi
                and int(item.rcpi) - client.rcpi
                >= -self.config.maximum_band_upgrade_loss_rcpi
            ]
            if not upgrades:
                return (
                    Decision(
                        action="none", reason="no_safe_band_upgrade",
                        scores=scores, **base,
                    ),
                    stable,
                )
            ranked = sorted(
                upgrades,
                key=lambda item: (-_BAND_RANK[item.band], -int(item.rcpi), item.bssid),
            )
            band_upgrade = True

        best: CandidateObservation = ranked[0]
        gain = int(best.rcpi) - client.rcpi
        if not band_upgrade and gain < self.config.minimum_target_gain_rcpi:
            return (
                Decision(action="none", reason="candidate_gain_too_small", scores=scores, **base),
                stable,
            )

        if (
            old.phase == "recommended"
            and old.source_bssid == client.connected_bssid
            and old.target_bssid == best.bssid
        ):
            held = (
                (now - parse_time(old.condition_since)).total_seconds()
                if old.condition_since else 0
            )
            return (
                Decision(
                    action="none",
                    reason="recommendation_unchanged",
                    target_bssid=best.bssid,
                    target_band=best.band,
                    target_rcpi=best.rcpi,
                    hold_seconds=held,
                    scores=scores,
                    **base,
                ),
                old,
            )

        same_condition = (
            old.phase == "holding"
            and old.source_bssid == client.connected_bssid
            and old.target_bssid == best.bssid
            and old.condition_since is not None
        )
        condition_since = old.condition_since if same_condition else snapshot.observed_at
        held = (now - parse_time(condition_since)).total_seconds()
        if held < self.config.condition_hold_seconds:
            new = ClientPolicyState(
                sta_mac=client.sta_mac,
                phase="holding",
                source_bssid=client.connected_bssid,
                target_bssid=best.bssid,
                condition_since=condition_since,
                last_action_at=old.last_action_at,
            )
            return (
                Decision(
                    action="none",
                    reason="condition_hold_not_met",
                    target_bssid=best.bssid,
                    target_band=best.band,
                    target_rcpi=best.rcpi,
                    hold_seconds=held,
                    scores=scores,
                    **base,
                ),
                new,
            )

        new = ClientPolicyState(
            sta_mac=client.sta_mac,
            phase="pending",
            source_bssid=client.connected_bssid,
            target_bssid=best.bssid,
            condition_since=condition_since,
            pending_since=snapshot.observed_at,
            failure_count=old.failure_count,
            last_failure_reason=old.last_failure_reason,
            last_action_at=snapshot.observed_at,
        )
        return (
            Decision(
                action="steer",
                reason=(
                    "band_preference_hold_satisfied"
                    if band_upgrade else "threshold_margin_hold_satisfied"
                ),
                target_bssid=best.bssid,
                target_band=best.band,
                target_rcpi=best.rcpi,
                hold_seconds=held,
                scores=scores,
                **base,
            ),
            new,
        )

    @staticmethod
    def _scores(
        candidates: list[CandidateObservation], client: ClientObservation
    ) -> tuple[CandidateScore, ...]:
        current_rank = _BAND_RANK.get(client.band)
        return tuple(
            CandidateScore(
                item.bssid,
                int(item.rcpi),
                int(item.rcpi) - int(client.rcpi),
                item.band,
                (
                    _BAND_RANK[item.band] - current_rank
                    if current_rank is not None and item.band in _BAND_RANK else 0
                ),
            )
            for item in candidates
        )
