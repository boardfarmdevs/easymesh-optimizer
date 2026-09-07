from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import re
from typing import Any, Iterable


_MAC = re.compile(r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$")
# EasyMesh/em_freq_band values are 0=2.4 GHz, 1=5 GHz, 2=60 GHz and
# 3=6 GHz. This lab intentionally has no 60 GHz policy domain.
_BAND_ALIASES = {
    0: "2.4",
    1: "5",
    3: "6",
    "0": "2.4",
    "1": "5",
    "3": "6",
    "2.4": "2.4",
    "2.4ghz": "2.4",
    "5": "5",
    "5ghz": "5",
    "6": "6",
    "6ghz": "6",
}


def normalize_mac(value: str) -> str:
    value = value.strip().lower()
    if not _MAC.fullmatch(value):
        raise ValueError(f"invalid MAC address: {value!r}")
    return value


def normalize_band(value: Any) -> str | None:
    if value is None or value == "":
        return None
    key = value.strip().lower() if isinstance(value, str) else value
    try:
        return _BAND_ALIASES[key]
    except (KeyError, TypeError) as error:
        raise ValueError(f"invalid Wi-Fi band: {value!r}") from error


def parse_time(value: str) -> datetime:
    if not value:
        raise ValueError("timestamp is empty")
    normalized = value.replace("Z", "+00:00")
    # Go's RFC3339Nano encoder can emit up to nine fractional digits, while
    # datetime.fromisoformat() accepts microseconds (six digits) at most.
    normalized = re.sub(r"(\.\d{6})\d+(?=[+-]\d\d:\d\d$)", r"\1", normalized)
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp has no timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def format_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True)
class CandidateObservation:
    sta_mac: str
    bssid: str
    device_id: str
    device_name: str
    rcpi: int | None
    metric_observed_at: str | None
    measurement_source: str
    band: str | None = None
    eligible: bool = True

    def __post_init__(self) -> None:
        normalize_mac(self.sta_mac)
        normalize_mac(self.bssid)
        if self.device_id:
            normalize_mac(self.device_id)
        if self.rcpi is not None and not 0 <= self.rcpi <= 220:
            raise ValueError("RCPI must be between 0 and 220")
        if self.metric_observed_at is not None:
            parse_time(self.metric_observed_at)
        object.__setattr__(self, "band", normalize_band(self.band))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CandidateObservation":
        return cls(**value)


@dataclass(frozen=True)
class ClientObservation:
    sta_mac: str
    connected_device_id: str
    connected_device_name: str
    connected_bssid: str
    rcpi: int | None
    association_uptime_seconds: int
    metric_observed_at: str | None
    measurement_source: str
    band: str | None = None
    ssid: str = ""
    cohort: str = "other"

    def __post_init__(self) -> None:
        normalize_mac(self.sta_mac)
        normalize_mac(self.connected_bssid)
        if self.connected_device_id:
            normalize_mac(self.connected_device_id)
        if self.rcpi is not None and not 0 <= self.rcpi <= 220:
            raise ValueError("RCPI must be between 0 and 220")
        if self.association_uptime_seconds < 0:
            raise ValueError("association uptime cannot be negative")
        if self.metric_observed_at is not None:
            parse_time(self.metric_observed_at)
        object.__setattr__(self, "band", normalize_band(self.band))
        expected_cohort = (
            "iot" if self.ssid == "iot_ssid"
            else "private" if self.ssid == "private_ssid"
            else "other"
        )
        if self.cohort not in {"private", "iot", "other"}:
            raise ValueError(f"invalid client cohort: {self.cohort!r}")
        if self.cohort != expected_cohort:
            raise ValueError(
                f"client cohort {self.cohort!r} does not match SSID {self.ssid!r}"
            )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ClientObservation":
        return cls(**value)


@dataclass(frozen=True)
class MeshHealth:
    devices: int | None
    clients: int | None
    radios: int | None = None
    bsses: int | None = None
    source: str = "controller_api"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MeshHealth":
        return cls(**value)


@dataclass(frozen=True)
class Snapshot:
    schema_version: int
    sequence: int
    observed_at: str
    controller_url: str
    health: MeshHealth
    clients: tuple[ClientObservation, ...]
    candidates: tuple[CandidateObservation, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError(f"unsupported snapshot schema {self.schema_version}")
        if self.sequence < 0:
            raise ValueError("snapshot sequence cannot be negative")
        parse_time(self.observed_at)
        client_macs = [client.sta_mac for client in self.clients]
        if len(client_macs) != len(set(client_macs)):
            raise ValueError("snapshot contains duplicate clients")

    def client(self, sta_mac: str) -> ClientObservation | None:
        sta_mac = normalize_mac(sta_mac)
        return next((item for item in self.clients if item.sta_mac == sta_mac), None)

    def candidates_for(self, sta_mac: str) -> tuple[CandidateObservation, ...]:
        sta_mac = normalize_mac(sta_mac)
        return tuple(item for item in self.candidates if item.sta_mac == sta_mac)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Snapshot":
        return cls(
            schema_version=value["schema_version"],
            sequence=value["sequence"],
            observed_at=value["observed_at"],
            controller_url=value.get("controller_url", "recorded"),
            health=MeshHealth.from_dict(value["health"]),
            clients=tuple(ClientObservation.from_dict(item) for item in value["clients"]),
            candidates=tuple(
                CandidateObservation.from_dict(item) for item in value.get("candidates", [])
            ),
        )


def sorted_clients(values: Iterable[ClientObservation]) -> tuple[ClientObservation, ...]:
    return tuple(sorted(values, key=lambda item: item.sta_mac))


def sorted_candidates(
    values: Iterable[CandidateObservation],
) -> tuple[CandidateObservation, ...]:
    return tuple(sorted(values, key=lambda item: (item.sta_mac, item.bssid)))
