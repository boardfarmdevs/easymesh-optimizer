from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ClientPolicyState:
    sta_mac: str
    phase: str = "stable"
    source_bssid: str | None = None
    target_bssid: str | None = None
    condition_since: str | None = None
    pending_since: str | None = None
    cooldown_until: str | None = None
    backoff_until: str | None = None
    failure_count: int = 0
    last_failure_reason: str | None = None
    last_action_at: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ClientPolicyState":
        return cls(**value)


@dataclass(frozen=True)
class PolicyState:
    clients: tuple[ClientPolicyState, ...] = ()

    def for_sta(self, sta_mac: str) -> ClientPolicyState:
        return next(
            (item for item in self.clients if item.sta_mac == sta_mac),
            ClientPolicyState(sta_mac=sta_mac),
        )

    def replace(self, value: ClientPolicyState) -> "PolicyState":
        retained = [item for item in self.clients if item.sta_mac != value.sta_mac]
        retained.append(value)
        return PolicyState(tuple(sorted(retained, key=lambda item: item.sta_mac)))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PolicyState":
        return cls(tuple(ClientPolicyState.from_dict(item) for item in value["clients"]))
