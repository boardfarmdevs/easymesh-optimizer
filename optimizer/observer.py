from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import json
import time
from typing import Any, Callable, Iterable
from urllib.request import urlopen

from .model import (
    CandidateObservation,
    ClientObservation,
    MeshHealth,
    Snapshot,
    format_time,
    normalize_band,
    normalize_mac,
    parse_time,
    sorted_candidates,
    sorted_clients,
)


JsonFetcher = Callable[[str], dict[str, Any]]
CandidateProvider = Callable[
    [
        tuple[ClientObservation, ...],
        tuple[CandidateObservation, ...],
        list[dict[str, Any]],
        str,
    ],
    Iterable[CandidateObservation],
]


def _default_fetch(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=5) as response:  # nosec: operator-supplied lab endpoint
        return json.load(response)


def _topology_client_context(topology: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for node in topology.get("nodes", []):
        for sta in node.get("STAList", []) or []:
            mac = sta.get("staMAC")
            if not mac:
                continue
            result[mac.lower()] = {
                "device_id": (node.get("id") or "").lower(),
                "device_name": node.get("name") or "",
                "band": normalize_band(sta.get("band")),
                "ssid": sta.get("ssid") or "",
            }
    return result


def _bss_inventory(
    payload: dict[str, Any], device_names: dict[str, str]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in payload.get("bsses", []):
        bssid = item.get("bssid")
        device_id = (item.get("device_id") or "").lower()
        if bssid:
            result.append(
                {
                    "bssid": normalize_mac(bssid),
                    "device_id": device_id,
                    "device_name": device_names.get(device_id, ""),
                    "band": normalize_band(item.get("band")),
                    "ssid": item.get("ssid") or "",
                }
            )
    return result


class ControllerObserver:
    """Read and normalize controller APIs without consulting simulator truth."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8888",
        *,
        fetcher: JsonFetcher | None = None,
        candidate_provider: CandidateProvider | None = None,
        current_link_fallback: Callable[[ClientObservation], ClientObservation] | None = None,
        trust_api_metric_timestamp: bool = True,
        max_current_metric_age_seconds: float | None = None,
        current_metric_floor: Callable[[ClientObservation], str | None] | None = None,
        current_link_progress: Callable[[ClientObservation], None] | None = None,
        fallback_executor=None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.fetcher = fetcher or _default_fetch
        self.candidate_provider = candidate_provider
        self.current_link_fallback = current_link_fallback
        self.trust_api_metric_timestamp = trust_api_metric_timestamp
        self.max_current_metric_age_seconds = max_current_metric_age_seconds
        self.current_metric_floor = current_metric_floor
        self.current_link_progress = current_link_progress
        self.fallback_executor = fallback_executor
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sequence = 0
        self.last_raw: dict[str, Any] | None = None

    def _get(self, path: str) -> dict[str, Any]:
        return self.fetcher(f"{self.base_url}{path}")

    def coordination_capabilities(self) -> dict[str, Any]:
        try:
            payload = self._get("/api/v1/coordination")
            if (payload.get("schema") == "easymesh.cli.coordination.v1"
                    and payload.get("candidate_wait_holds_native_lock") is False
                    and payload.get("native_command_and_tree_ownership_serialized") is True
                    and type(payload.get("candidate_parallel_agents")) is int
                    and 1 <= payload["candidate_parallel_agents"] <= 5):
                return payload
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return {"candidate_parallel_agents": 1, "negotiated": False}

    def association(self, sta_mac: str) -> str | None:
        sta_mac = normalize_mac(sta_mac)
        topology = self._get("/api/v1/topology")
        matches = [station for node in topology.get("nodes", [])
                   for station in node.get("STAList", []) or []
                   if str(station.get("staMAC", "")).lower() == sta_mac]
        if len(matches) != 1 or not matches[0].get("bssid"):
            return None
        return normalize_mac(matches[0]["bssid"])

    def observe_topology(self) -> Snapshot:
        topology = self._get("/api/v1/topology")
        if not topology.get("nodes"):
            raise ValueError("controller topology is unavailable")
        clients = []
        for node in topology.get("nodes", []):
            for station in node.get("STAList", []) or []:
                ssid = station.get("ssid") or ""
                clients.append(ClientObservation(
                    sta_mac=normalize_mac(station["staMAC"]),
                    connected_device_id=normalize_mac(node["id"]),
                    connected_device_name=node.get("name") or "",
                    connected_bssid=normalize_mac(station["bssid"]),
                    rcpi=None, metric_observed_at=None, association_uptime_seconds=0,
                    measurement_source="controller_topology_without_metrics",
                    band=normalize_band(station.get("band")), ssid=ssid,
                    cohort="iot" if ssid == "iot_ssid" else "private" if ssid == "private_ssid" else "other",
                ))
        observed_at = format_time(self.clock())
        snapshot = Snapshot(
            schema_version=1, sequence=self.sequence, observed_at=observed_at,
            controller_url=self.base_url, clients=sorted_clients(clients), candidates=(),
            health=MeshHealth(devices=sum(bool(node.get("haulTypes")) for node in topology.get("nodes", [])),
                              clients=len(clients)),
        )
        self.sequence += 1
        self.last_raw = {"topology": topology, "sampled_at": observed_at}
        return snapshot

    def metrics_for(self, clients) -> tuple[ClientObservation, ...]:
        payload = self._get("/api/v1/clients")
        by_mac = {}
        for item in payload.get("clients", []):
            mac = normalize_mac(item["mac"])
            if mac in by_mac:
                raise ValueError("duplicate client metric ownership")
            by_mac[mac] = item
        measured = []
        for client in clients:
            item = by_mac.get(client.sta_mac, {})
            metric = item.get("client_metrics") or {}
            timestamp = metric.get("last_updated")
            if (item.get("connected_bssid", "").lower() == client.connected_bssid
                    and metric.get("rcpi") not in (None, 0) and timestamp and not timestamp.startswith("0001-")):
                client = replace(client, rcpi=int(metric["rcpi"]), metric_observed_at=timestamp,
                                 association_uptime_seconds=int(metric.get("association_uptime_seconds") or 0),
                                 measurement_source="associated_sta_link_metrics")
            measured.append(client)
        return self._refresh_current(measured)

    def _refresh_current(self, clients):
        def refresh(client):
            floor = self.current_metric_floor(client) if self.current_metric_floor else None
            stale = client.rcpi is None or client.metric_observed_at is None or (
                self.max_current_metric_age_seconds is not None and not
                -5 <= (self.clock() - parse_time(client.metric_observed_at)).total_seconds()
                <= self.max_current_metric_age_seconds
            ) or (floor is not None and parse_time(client.metric_observed_at) <= parse_time(floor))
            measured = self.current_link_fallback(client) if stale and self.current_link_fallback else client
            if self.current_link_progress:
                self.current_link_progress(measured)
            return measured
        refreshed = self.fallback_executor.map(refresh, clients) if self.fallback_executor else map(refresh, clients)
        return sorted_clients(refreshed)

    def observe(self) -> Snapshot:
        started = time.monotonic()
        sample_started_at = format_time(self.clock())
        topology = self._get("/api/v1/topology")
        clients_payload = self._get("/api/v1/clients")
        devices_payload = self._get("/api/v1/devices")
        bsses_payload = self._get("/api/v1/bsses")
        self.last_raw = {
            "sample_started_at": sample_started_at,
            "topology": topology,
            "clients": clients_payload,
            "devices": devices_payload,
            "bsses": bsses_payload,
        }
        context = _topology_client_context(topology)
        devices = devices_payload.get("devices", [])
        device_names = {
            (item.get("mac") or "").lower(): item.get("role") or ""
            for item in devices
            if item.get("mac")
        }
        bsses = _bss_inventory(bsses_payload, device_names)
        bss_by_id = {item["bssid"]: item for item in bsses}

        clients: list[ClientObservation] = []
        client_context: dict[str, dict[str, Any]] = {}
        for item in clients_payload.get("clients", []):
            mac = normalize_mac(item["mac"])
            topology_placement = context.get(mac, {})
            connected_bssid = normalize_mac(item["connected_bssid"])
            connected_bss = bss_by_id.get(connected_bssid, {})
            device_id = (
                item.get("connected_ap_mac")
                or connected_bss.get("device_id")
                or topology_placement.get("device_id")
                or ""
            ).lower()
            placement = {
                "device_id": device_id,
                "device_name": (
                    device_names.get(device_id)
                    or connected_bss.get("device_name")
                    or topology_placement.get("device_name")
                    or ""
                ),
                "band": connected_bss.get("band") or topology_placement.get("band"),
                "ssid": connected_bss.get("ssid") or topology_placement.get("ssid") or "",
            }
            client_context[mac] = placement
            metrics = item.get("client_metrics") or {}
            raw_rcpi = metrics.get("rcpi")
            rcpi = int(raw_rcpi) if raw_rcpi not in (None, 0) else None
            raw_metric_time = metrics.get("last_updated")
            metric_time = (
                raw_metric_time
                if self.trust_api_metric_timestamp
                and raw_metric_time
                and not raw_metric_time.startswith("0001-")
                else None
            )
            clients.append(
                ClientObservation(
                    sta_mac=mac,
                    connected_device_id=(
                        item.get("connected_ap_mac")
                        or placement.get("device_id")
                        or ""
                    ).lower(),
                    connected_device_name=placement.get("device_name") or "",
                    connected_bssid=connected_bssid,
                    rcpi=rcpi,
                    association_uptime_seconds=int(
                        metrics.get("association_uptime_seconds") or 0
                    ),
                    metric_observed_at=metric_time,
                    measurement_source="associated_sta_link_metrics",
                    band=placement.get("band"),
                    ssid=placement.get("ssid") or "",
                    cohort=(
                        "iot" if placement.get("ssid") == "iot_ssid"
                        else "private" if placement.get("ssid") == "private_ssid"
                        else "other"
                    ),
                )
            )
        normalized_clients = self._refresh_current(clients)

        if self.current_metric_floor is not None:
            normalized_clients = sorted_clients(
                replace(client, rcpi=None, metric_observed_at=None)
                if (floor := self.current_metric_floor(client)) is not None and (
                    client.metric_observed_at is None or parse_time(client.metric_observed_at) <= parse_time(floor)
                ) else client for client in normalized_clients
            )

        candidates: list[CandidateObservation] = []
        for client in normalized_clients:
            placement = client_context.get(client.sta_mac, {})
            for bss in bsses:
                if bss["bssid"] == client.connected_bssid:
                    continue
                if placement and bss["ssid"] != placement.get("ssid"):
                    continue
                candidates.append(
                    CandidateObservation(
                        sta_mac=client.sta_mac,
                        bssid=bss["bssid"],
                        device_id=bss["device_id"],
                        device_name=bss["device_name"],
                        rcpi=None,
                        metric_observed_at=None,
                        measurement_source="controller_bss_inventory_only",
                        band=bss["band"],
                    )
                )
        if self.candidate_provider is not None:
            inventory = sorted_candidates(candidates)
            measured = list(
                self.candidate_provider(
                    normalized_clients,
                    inventory,
                    bsses_payload.get("bsses", []),
                    format_time(self.clock()) if self.current_link_fallback else sample_started_at,
                )
            )
            measured_keys = {(item.sta_mac, item.bssid) for item in measured}
            rejected_keys = set(getattr(
                self.candidate_provider, "last_rejected_candidate_keys", set()
            ))
            candidates = [
                item
                for item in candidates
                if (item.sta_mac, item.bssid) not in measured_keys
                and (item.sta_mac, item.bssid) not in rejected_keys
            ] + measured
            provider_raw = getattr(self.candidate_provider, "last_raw", None)
            if provider_raw is not None:
                self.last_raw["candidate_transactions"] = provider_raw
            if rejected_keys:
                self.last_raw["rejected_candidate_keys"] = sorted(rejected_keys)

        # Active candidate queries finish after the passive API reads. The
        # immutable snapshot represents the completed observation interval;
        # dating it at interval start would make freshly received candidate
        # timestamps appear to come from the future and fail the freshness
        # gate.
        observed_at = format_time(self.clock())
        self.last_raw["sampled_at"] = observed_at
        self.last_raw["observation_elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)

        mesh_devices = sum(
            1 for item in devices if (item.get("role") or "").lower() != "controller"
        )
        snapshot = Snapshot(
            schema_version=1,
            sequence=self.sequence,
            observed_at=observed_at,
            controller_url=self.base_url,
            health=MeshHealth(
                devices=mesh_devices,
                clients=len(normalized_clients),
                radios=None,
                bsses=len(bsses) if bsses else None,
            ),
            clients=normalized_clients,
            candidates=sorted_candidates(candidates),
        )
        self.sequence += 1
        return snapshot
