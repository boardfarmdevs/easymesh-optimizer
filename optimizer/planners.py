from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .model import normalize_band, normalize_mac, parse_time


@dataclass(frozen=True)
class BackhaulPlannerConfig:
    maximum_metric_age_seconds: float = 15
    snr_weight: float = 1
    phy_rate_weight: float = 0.02
    utilization_weight: float = 0.20
    retry_weight: float = 0.50
    band_bonus_24: float = -12
    band_bonus_5: float = 4
    band_bonus_6: float = 6
    minimum_snr_db: int = 5

    def band_bonus(self, band: str) -> float:
        return {
            "2.4": self.band_bonus_24,
            "5": self.band_bonus_5,
            "6": self.band_bonus_6,
        }[band]


@dataclass(frozen=True)
class BackhaulEdgeObservation:
    left: str
    right: str
    band: str
    snr_db: int
    phy_rate_mbps: float
    utilization_percent: float
    retry_percent: float
    observed_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "left", normalize_mac(self.left))
        object.__setattr__(self, "right", normalize_mac(self.right))
        object.__setattr__(self, "band", normalize_band(self.band))
        if self.left == self.right:
            raise ValueError("backhaul edge cannot be a self-loop")
        if self.band not in {"2.4", "5", "6"}:
            raise ValueError("backhaul edge requires a supported band")
        if not -20 <= self.snr_db <= 60:
            raise ValueError("backhaul SNR must be in [-20, 60]")
        if self.phy_rate_mbps < 0:
            raise ValueError("backhaul PHY rate cannot be negative")
        for name, value in (
            ("utilization", self.utilization_percent),
            ("retry", self.retry_percent),
        ):
            if not 0 <= value <= 100:
                raise ValueError(f"backhaul {name} must be in [0, 100]")
        parse_time(self.observed_at)


@dataclass(frozen=True)
class PlannedBackhaulEdge:
    left: str
    right: str
    band: str
    score: float
    snr_db: int
    phy_rate_mbps: float
    utilization_percent: float
    retry_percent: float


@dataclass(frozen=True)
class BackhaulPlan:
    status: str
    reason: str
    root: str
    nodes: tuple[str, ...]
    edges: tuple[PlannedBackhaulEdge, ...]
    total_score: float
    rejected_stale: int = 0
    rejected_weak: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _DisjointSet:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: str, right: str) -> bool:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return False
        self.parent[right_root] = left_root
        return True


def plan_backhaul(
    *,
    root: str,
    nodes: Iterable[str],
    edges: Iterable[BackhaulEdgeObservation],
    observed_at: str,
    config: BackhaulPlannerConfig | None = None,
) -> BackhaulPlan:
    """Choose a maximum-utility loop-free undirected backhaul tree."""
    config = config or BackhaulPlannerConfig()
    root = normalize_mac(root)
    normalized_nodes = tuple(sorted({normalize_mac(item) for item in nodes}))
    if root not in normalized_nodes:
        raise ValueError("backhaul root is not in nodes")
    now = parse_time(observed_at)
    candidates = []
    stale = 0
    weak = 0
    for edge in edges:
        if edge.left not in normalized_nodes or edge.right not in normalized_nodes:
            raise ValueError("backhaul edge references a node outside the graph")
        age = (now - parse_time(edge.observed_at)).total_seconds()
        if age < 0 or age > config.maximum_metric_age_seconds:
            stale += 1
            continue
        if edge.snr_db < config.minimum_snr_db:
            weak += 1
            continue
        score = (
            config.snr_weight * edge.snr_db
            + config.phy_rate_weight * edge.phy_rate_mbps
            - config.utilization_weight * edge.utilization_percent
            - config.retry_weight * edge.retry_percent
            + config.band_bonus(edge.band)
        )
        candidates.append((score, edge))
    candidates.sort(
        key=lambda item: (
            -item[0],
            min(item[1].left, item[1].right),
            max(item[1].left, item[1].right),
            item[1].band,
        )
    )
    sets = _DisjointSet(normalized_nodes)
    selected = []
    for score, edge in candidates:
        if not sets.union(edge.left, edge.right):
            continue
        selected.append(
            PlannedBackhaulEdge(
                left=min(edge.left, edge.right),
                right=max(edge.left, edge.right),
                band=edge.band,
                score=round(score, 3),
                snr_db=edge.snr_db,
                phy_rate_mbps=edge.phy_rate_mbps,
                utilization_percent=edge.utilization_percent,
                retry_percent=edge.retry_percent,
            )
        )
        if len(selected) == len(normalized_nodes) - 1:
            break
    connected = len(selected) == max(0, len(normalized_nodes) - 1)
    return BackhaulPlan(
        status="recommend" if connected else "blocked",
        reason="maximum_utility_spanning_tree" if connected else "fresh_viable_graph_disconnected",
        root=root,
        nodes=normalized_nodes,
        edges=tuple(selected if connected else ()),
        total_score=round(sum(item.score for item in selected), 3) if connected else 0,
        rejected_stale=stale,
        rejected_weak=weak,
    )


@dataclass(frozen=True)
class RadioEnvironmentObservation:
    radio_id: str
    band: str
    current_width_mhz: int
    allowed_widths_mhz: tuple[int, ...]
    primary_utilization_percent: float
    secondary_utilization_percent: float
    overlapping_neighbor_percent: float
    radar_risk: float
    observed_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "radio_id", normalize_mac(self.radio_id))
        object.__setattr__(self, "band", normalize_band(self.band))
        allowed = tuple(sorted(set(int(item) for item in self.allowed_widths_mhz)))
        if self.band not in {"2.4", "5", "6"}:
            raise ValueError("radio environment requires a supported band")
        if not allowed or any(item not in {20, 40, 80, 160} for item in allowed):
            raise ValueError("allowed channel widths must be drawn from 20/40/80/160")
        if self.current_width_mhz not in allowed:
            raise ValueError("current width must be in allowed widths")
        object.__setattr__(self, "allowed_widths_mhz", allowed)
        for name, value in (
            ("primary utilization", self.primary_utilization_percent),
            ("secondary utilization", self.secondary_utilization_percent),
            ("overlapping neighbors", self.overlapping_neighbor_percent),
        ):
            if not 0 <= value <= 100:
                raise ValueError(f"{name} must be in [0, 100]")
        if not 0 <= self.radar_risk <= 1:
            raise ValueError("radar risk must be in [0, 1]")
        parse_time(self.observed_at)


@dataclass(frozen=True)
class ChannelWidthRecommendation:
    action: str
    reason: str
    radio_id: str
    band: str
    current_width_mhz: int
    target_width_mhz: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def recommend_channel_width(
    observation: RadioEnvironmentObservation,
    *,
    now: str,
    maximum_metric_age_seconds: float = 30,
) -> ChannelWidthRecommendation:
    age = (parse_time(now) - parse_time(observation.observed_at)).total_seconds()
    base = dict(
        radio_id=observation.radio_id,
        band=observation.band,
        current_width_mhz=observation.current_width_mhz,
    )
    if age < 0 or age > maximum_metric_age_seconds:
        return ChannelWidthRecommendation(
            action="none", reason="radio_environment_stale", target_width_mhz=None, **base
        )
    allowed = observation.allowed_widths_mhz
    if observation.band == "2.4":
        desired = 40 if (
            40 in allowed
            and observation.primary_utilization_percent <= 25
            and observation.secondary_utilization_percent <= 20
            and observation.overlapping_neighbor_percent <= 20
        ) else min(allowed)
        reason = "clean_24ghz_location" if desired == 40 else "protect_24ghz_coexistence"
    elif observation.band == "5":
        if observation.radar_risk >= 0.5:
            desired = max(item for item in allowed if item <= 40)
            reason = "radar_risk_width_cap"
        elif (
            observation.primary_utilization_percent <= 50
            and observation.secondary_utilization_percent <= 20
            and observation.overlapping_neighbor_percent <= 30
        ):
            desired = max(allowed)
            reason = "clean_5ghz_wide_channel"
        elif observation.primary_utilization_percent >= 75:
            desired = max(item for item in allowed if item <= 40)
            reason = "high_5ghz_congestion"
        else:
            desired = max(item for item in allowed if item <= 80)
            reason = "balanced_5ghz_width"
    else:
        if (
            observation.primary_utilization_percent <= 60
            and observation.secondary_utilization_percent <= 35
            and observation.overlapping_neighbor_percent <= 35
        ):
            desired = max(allowed)
            reason = "clean_6ghz_wide_channel"
        else:
            desired = max(item for item in allowed if item <= 80)
            reason = "congested_6ghz_width_cap"
    if desired == observation.current_width_mhz:
        return ChannelWidthRecommendation(
            action="none", reason=f"{reason}_already_set", target_width_mhz=None, **base
        )
    return ChannelWidthRecommendation(
        action="recommend", reason=reason, target_width_mhz=desired, **base
    )
