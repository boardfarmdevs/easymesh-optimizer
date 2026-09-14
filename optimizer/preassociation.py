from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

from .model import normalize_band, normalize_mac, parse_time


_BAND_RANK = {"2.4": 0, "5": 1, "6": 2}


@dataclass(frozen=True)
class PreAssociationConfig:
    maximum_suppression_seconds: float = 3
    maximum_suppressed_probes: int = 3
    failsafe_cooldown_seconds: float = 30

    def __post_init__(self) -> None:
        if self.maximum_suppression_seconds <= 0:
            raise ValueError("maximum suppression window must be positive")
        if self.maximum_suppressed_probes <= 0:
            raise ValueError("maximum suppressed probes must be positive")
        if self.failsafe_cooldown_seconds < 0:
            raise ValueError("failsafe cooldown cannot be negative")


@dataclass(frozen=True)
class ProbeObservation:
    sta_mac: str
    bssid: str
    band: str
    observed_at: str
    known_supported_bands: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "sta_mac", normalize_mac(self.sta_mac))
        object.__setattr__(self, "bssid", normalize_mac(self.bssid))
        object.__setattr__(self, "band", normalize_band(self.band))
        bands = tuple(
            sorted(
                {normalize_band(item) for item in self.known_supported_bands},
                key=lambda item: _BAND_RANK[item],
            )
        )
        if self.band not in _BAND_RANK or any(item not in _BAND_RANK for item in bands):
            raise ValueError("probe uses an unsupported band")
        object.__setattr__(self, "known_supported_bands", bands)
        parse_time(self.observed_at)


@dataclass(frozen=True)
class PreAssociationClientState:
    sta_mac: str
    window_started_at: str | None = None
    suppressed_probes: int = 0
    cooldown_until: str | None = None


@dataclass(frozen=True)
class PreAssociationState:
    clients: tuple[PreAssociationClientState, ...] = ()

    def for_sta(self, sta_mac: str) -> PreAssociationClientState:
        sta_mac = normalize_mac(sta_mac)
        return next(
            (item for item in self.clients if item.sta_mac == sta_mac),
            PreAssociationClientState(sta_mac=sta_mac),
        )

    def replace(self, value: PreAssociationClientState) -> "PreAssociationState":
        retained = [item for item in self.clients if item.sta_mac != value.sta_mac]
        retained.append(value)
        return PreAssociationState(tuple(sorted(retained, key=lambda item: item.sta_mac)))


@dataclass(frozen=True)
class ProbeDecision:
    action: str
    reason: str
    sta_mac: str
    bssid: str
    band: str
    preferred_band: str | None
    suppressed_probes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PreAssociationPolicy:
    """Bounded probe-response preference policy; no live action adapter."""

    def __init__(self, config: PreAssociationConfig | None = None) -> None:
        self.config = config or PreAssociationConfig()

    def evaluate(
        self, observation: ProbeObservation, state: PreAssociationState | None = None
    ) -> tuple[ProbeDecision, PreAssociationState]:
        state = state or PreAssociationState()
        old = state.for_sta(observation.sta_mac)
        now = parse_time(observation.observed_at)
        preferred = max(
            (item for item in observation.known_supported_bands if item != "2.4"),
            key=lambda item: _BAND_RANK[item],
            default=None,
        )
        base = dict(
            sta_mac=observation.sta_mac,
            bssid=observation.bssid,
            band=observation.band,
            preferred_band=preferred,
        )

        if preferred is None:
            decision = ProbeDecision(
                action="allow_probe_response",
                reason="higher_band_support_unknown",
                suppressed_probes=0,
                **base,
            )
            return decision, state.replace(PreAssociationClientState(observation.sta_mac))

        if observation.band != "2.4":
            decision = ProbeDecision(
                action="allow_probe_response",
                reason="preferred_band_probe_observed",
                suppressed_probes=0,
                **base,
            )
            return decision, state.replace(PreAssociationClientState(observation.sta_mac))

        if old.cooldown_until and now < parse_time(old.cooldown_until):
            decision = ProbeDecision(
                action="allow_probe_response",
                reason="failsafe_cooldown",
                suppressed_probes=old.suppressed_probes,
                **base,
            )
            return decision, state

        started = parse_time(old.window_started_at) if old.window_started_at else now
        age = (now - started).total_seconds()
        limit_reached = (
            age >= self.config.maximum_suppression_seconds
            or old.suppressed_probes >= self.config.maximum_suppressed_probes
        )
        if limit_reached:
            until = now + timedelta(seconds=self.config.failsafe_cooldown_seconds)
            new = PreAssociationClientState(
                sta_mac=observation.sta_mac,
                suppressed_probes=old.suppressed_probes,
                cooldown_until=until.isoformat(),
            )
            decision = ProbeDecision(
                action="allow_probe_response",
                reason="bounded_24ghz_failsafe",
                suppressed_probes=old.suppressed_probes,
                **base,
            )
            return decision, state.replace(new)

        count = old.suppressed_probes + 1
        new = PreAssociationClientState(
            sta_mac=observation.sta_mac,
            window_started_at=started.isoformat(),
            suppressed_probes=count,
        )
        decision = ProbeDecision(
            action="suppress_probe_response",
            reason="bounded_higher_band_preference",
            suppressed_probes=count,
            **base,
        )
        return decision, state.replace(new)
