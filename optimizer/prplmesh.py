from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from dataclasses import replace
import json
import re
import subprocess
import time
from typing import Any, Callable, Iterable
from urllib.request import urlopen

from .candidates import CandidateMetricsError, CandidateMetricsUnavailable, CandidateSnapshotSuperseded
from .lxd_ubus import ControllerNamespaceChanged, LxdUbusTransport
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


def _band(value: str | None) -> str | None:
    return normalize_band(value.replace(" ", "") if value else value)


def _rcpi(value: Any) -> int | None:
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            return None
    return value if type(value) is int and 0 <= value <= 220 else None


def _fetch(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=5) as response:  # nosec: operator lab endpoint
        return json.load(response)


def _flatten(topology: dict[str, Any]) -> tuple[list[dict], list[dict], list[dict]]:
    devices: list[dict] = []
    bsses: list[dict] = []
    clients: list[dict] = []
    for device in topology.get("devices", []):
        device_id = normalize_mac(device["id"])
        devices.append({"id": device_id, "name": device.get("name", "")})
        for radio in device.get("radios", []):
            radio_id = normalize_mac(radio["id"])
            band = _band(radio.get("band"))
            for bss in radio.get("bsses", []):
                record = {
                    "bssid": normalize_mac(bss["bssid"]),
                    "device_id": device_id,
                    "device_name": device.get("name", ""),
                    "radio_id": radio_id,
                    "band": band,
                    "channel": int(radio.get("channel") or 0),
                    "opclass": int(radio.get("opclass") or 0),
                    "ssid": bss.get("ssid") or "",
                }
                bsses.append(record)
                for client in bss.get("clients", []):
                    clients.append({**client, **record})
    return devices, bsses, clients


class PrplMeshObserver:
    """Normalize the prplMesh topology adapter without reading RF truth."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8092",
        *,
        fetcher=None,
        candidate_provider=None,
        ownership_observer=None,
        current_link_fallback=None,
        trust_api_metric_timestamp: bool = True,
        max_current_metric_age_seconds=None,
        current_metric_floor=None,
        current_link_progress=None,
        fallback_executor=None,
        clock=None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.fetcher = fetcher or _fetch
        self.candidate_provider = candidate_provider
        self.ownership_observer = ownership_observer
        self.current_link_fallback = current_link_fallback
        self.trust_api_metric_timestamp = trust_api_metric_timestamp
        self.max_current_metric_age_seconds = max_current_metric_age_seconds
        self.current_metric_floor = current_metric_floor
        self.current_link_progress = current_link_progress
        self.fallback_executor = fallback_executor
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sequence = 0
        self.last_raw: dict[str, Any] | None = None

    def coordination_capabilities(self):
        return {"candidate_parallel_agents": 1, "negotiated": False,
                "backend": "prplmesh_nbapi", "candidate_publication": "per_radio",
                "native_autonomous_optimizer_claim": False}

    def association(self, sta_mac):
        topology = self.fetcher(f"{self.base_url}/api/topology")
        clients = _flatten(topology)[2]
        matches = [client for client in clients if normalize_mac(client["id"]) == normalize_mac(sta_mac)]
        return matches[0]["bssid"] if len(matches) == 1 else None

    def observe_topology(self):
        return self.observe(include_candidates=False, refresh_current=False)

    def metrics_for(self, clients):
        snapshot = self.observe(include_candidates=False, refresh_current=False)
        observed = {client.sta_mac: client for client in snapshot.clients}
        measured = []
        for client in clients:
            metric = observed.get(client.sta_mac)
            measured.append(metric if metric and metric.connected_bssid == client.connected_bssid
                            and metric.band == client.band else client)
        return self._refresh_current(measured)

    def _refresh_current(self, clients):
        def refresh(client):
            timestamp = client.metric_observed_at
            floor = self.current_metric_floor(client) if self.current_metric_floor else None
            stale = client.rcpi is None or timestamp is None or (
                self.max_current_metric_age_seconds is not None and not
                -5 <= (self.clock() - parse_time(timestamp)).total_seconds() <= self.max_current_metric_age_seconds
            ) or (floor is not None and parse_time(timestamp) <= parse_time(floor))
            measured = self.current_link_fallback(client) if stale and self.current_link_fallback else client
            if self.current_link_progress:
                self.current_link_progress(measured)
            return measured
        return sorted_clients(self.fallback_executor.map(refresh, clients)
                              if self.fallback_executor else map(refresh, clients))

    def observe(self, *, include_candidates=True, refresh_current=True) -> Snapshot:
        for attempt in range(3):
            try:
                topology = self.fetcher(f"{self.base_url}/api/topology")
            except OSError as error:
                raise CandidateMetricsUnavailable(
                    f"prplMesh topology collection failed: {type(error).__name__}: {error}"
                ) from error
            devices, bsses, raw_clients = _flatten(topology)
            identities = [normalize_mac(client["id"]) for client in raw_clients]
            if len(identities) == len(set(identities)):
                break
            if attempt < 2:
                time.sleep(0.5)
        else:
            raise CandidateMetricsUnavailable(
                "prplMesh topology changed during collection: duplicate client ownership"
            )
        sampled_at = topology.get("generated_at") or format_time(self.clock())
        clients: list[ClientObservation] = []
        for item in raw_clients:
            rcpi_value = _rcpi(item.get("signal_raw"))
            metric_time = item.get("signal_updated_at")
            clients.append(
                ClientObservation(
                    sta_mac=normalize_mac(item["id"]),
                    connected_device_id=item["device_id"],
                    connected_device_name=item["device_name"],
                    connected_bssid=item["bssid"],
                    rcpi=rcpi_value,
                    association_uptime_seconds=int(
                        item.get("last_connect_seconds") or 0
                    ),
                    metric_observed_at=(
                        metric_time if self.trust_api_metric_timestamp else None
                    ),
                    measurement_source="prplmesh_associated_sta_link_metrics",
                    band=item["band"],
                    ssid=item["ssid"],
                    cohort=(
                        "iot" if item["ssid"] == "iot_ssid"
                        else "private" if item["ssid"] == "private_ssid"
                        else "other"
                    ),
                )
            )
        self.last_raw = {"sample_started_at": sampled_at, "topology": topology}
        if self.ownership_observer is not None:
            self.ownership_observer(sorted_clients(clients), self.last_raw)
        normalized_clients = self._refresh_current(clients) if refresh_current else sorted_clients(clients)
        candidates: list[CandidateObservation] = []
        for client in normalized_clients:
            for bss in bsses:
                if bss["bssid"] == client.connected_bssid or bss["ssid"] != client.ssid:
                    continue
                candidates.append(
                    CandidateObservation(
                        sta_mac=client.sta_mac,
                        bssid=bss["bssid"],
                        device_id=bss["device_id"],
                        device_name=bss["device_name"],
                        rcpi=None,
                        metric_observed_at=None,
                        measurement_source="prplmesh_bss_inventory_only",
                        band=bss["band"],
                    )
                )
        provider_raw = None
        if self.candidate_provider is not None and include_candidates:
            measured = list(
                self.candidate_provider(
                    normalized_clients,
                    sorted_candidates(candidates),
                    bsses,
                    format_time(self.clock()) if self.current_link_fallback else sampled_at,
                )
            )
            measured_keys = {(item.sta_mac, item.bssid) for item in measured}
            candidates = [
                item
                for item in candidates
                if (item.sta_mac, item.bssid) not in measured_keys
            ] + measured
            provider_raw = getattr(self.candidate_provider, "last_raw", None)

        observed_at = format_time(self.clock())
        self.last_raw["sampled_at"] = observed_at
        if provider_raw is not None:
            self.last_raw["candidate_transactions"] = provider_raw
        snapshot = Snapshot(
            schema_version=1,
            sequence=self.sequence,
            observed_at=observed_at,
            controller_url=self.base_url,
            health=MeshHealth(
                devices=len(devices),
                clients=len(normalized_clients),
                radios=len({bss["radio_id"] for bss in bsses}),
                bsses=len(bsses),
                source="prplmesh_nbapi",
            ),
            clients=normalized_clients,
            candidates=sorted_candidates(candidates),
        )
        self.sequence += 1
        return snapshot


def _first_json(text: str) -> dict[str, Any]:
    try:
        value, _ = json.JSONDecoder().raw_decode(text.lstrip())
    except json.JSONDecodeError as error:
        raise CandidateMetricsError(f"invalid prplMesh NBAPI response: {text!r}") from error
    if not isinstance(value, dict):
        raise CandidateMetricsError("prplMesh NBAPI response is not an object")
    return value


class PrplMeshCandidateProvider:
    """Collect candidate RCPI through prplMesh's standard NBAPI transaction."""

    NETWORK = "Device.WiFi.DataElements.Network"
    # A streaming round asks for one band's clients at a time, as the prplMesh
    # lab was qualified; the RDK provider takes a cohort across bands.
    same_band_cohort = True

    def __init__(
        self,
        *,
        controller: str = "prpl-controller",
        allow_simulated: bool = False,
        timeout_seconds: float = 30,
        client_selector: Callable[[ClientObservation, str], bool] | None = None,
        generation_guard=None,
        client_prioritizer=None,
        progress=None,
        result_ready=None,
        batch_radio_reads: bool = True,
    ) -> None:
        self.controller = controller
        self.transport = LxdUbusTransport(controller)
        self.allow_simulated = allow_simulated
        self.timeout_seconds = timeout_seconds
        self.client_selector = client_selector
        self.generation_guard = generation_guard
        self.client_prioritizer = client_prioritizer
        self.progress = progress or (lambda _value: None)
        self.result_ready = result_ready
        self.batch_radio_reads = batch_radio_reads
        self.registered: set[tuple[str, str]] = set()
        self.registration_channels: dict[str, frozenset[tuple[int, int]]] = {}
        self.object_cache: dict[tuple[str, str], str] = {}
        self.defer_registration_query = False
        self.last_raw: list[dict[str, Any]] = []
        self.last_selected_sta_macs: set[str] = set()
        self.last_requested_sta_macs: set[str] = set()
        self.last_rejected_candidate_keys = set()
        self.last_selection = {}

    def _call(self, obj: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.generation_guard is not None and not self.generation_guard():
            raise CandidateSnapshotSuperseded("prplMesh candidate generation was superseded")
        started = time.monotonic()
        transaction = {"operation": "nbapi", "object": obj, "method": method, "transport": self.transport.name,
                       "requested_at": format_time(datetime.now(timezone.utc))}
        self.last_raw.append(transaction)
        try:
            completed = self.transport(obj, method, payload)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError,
                ControllerNamespaceChanged) as error:
            transaction["error"] = str(error)
            raise CandidateMetricsUnavailable(
                f"prplMesh NBAPI call failed: {obj} {method}"
            ) from error
        finally:
            transaction["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
            transaction["finished_at"] = format_time(datetime.now(timezone.utc))
        return _first_json(completed.stdout)

    def _radio_objects(self) -> dict[tuple[str, str], str]:
        values = self._call(self.NETWORK, "_get", {"rel_path": "Device.*.", "depth": 2})
        device_pattern = re.escape(self.NETWORK) + r"\.Device\.\d+"
        device_ids = {
            path.rstrip("."): normalize_mac(value["ID"])
            for path, value in values.items()
            if re.fullmatch(device_pattern, path.rstrip("."))
        }
        discovered = {}
        for path, value in values.items():
            radio = path.rstrip(".")
            if not re.fullmatch(device_pattern + r"\.Radio\.\d+", radio):
                continue
            device = radio.split(".Radio.", 1)[0]
            device_id = device_ids.get(device)
            if device_id is None:
                raise CandidateMetricsUnavailable(
                    f"NBAPI radio {radio} has no discovered parent device"
                )
            radio_id = normalize_mac(value["ID"])
            discovered[(device_id, radio_id)] = radio
        if not discovered:
            raise CandidateMetricsUnavailable("NBAPI radio inventory is empty")
        if discovered == self.object_cache:
            return self.object_cache
        self.object_cache.clear()
        self.registered.clear()
        self.registration_channels.clear()
        description = self._call(next(iter(discovered.values())), "_describe",
                                 {"functions": True, "parameters": False, "objects": False})
        arguments = description.get("functions", {}).get("AddUnassociatedStation", {}).get("arguments", [])
        self.defer_registration_query = any(argument.get("name") == "defer_query"
                                             and argument.get("type_name") == "bool"
                                             for argument in arguments)
        self.object_cache = discovered
        return self.object_cache

    def _radio_metrics(self, radio: str) -> dict[str, tuple[int, str]]:
        value = self._call(
            radio + ".UnassociatedSTA",
            "_get",
            {"rel_path": "", "depth": 2},
        )
        return self._parse_radio_metrics(value)

    @staticmethod
    def _parse_radio_metrics(value) -> dict[str, tuple[int, str]]:
        result = {}
        for key, item in value.items():
            if not key.rstrip(".").split(".")[-1].isdigit() or not isinstance(item, dict):
                continue
            mac = item.get("MACAddress")
            timestamp = item.get("X_PRPLWARE-COM_TimeStamp")
            signal = _rcpi(item.get("SignalStrength"))
            if mac and timestamp and not str(timestamp).startswith("0001-") and signal is not None:
                result[normalize_mac(mac)] = (signal, str(timestamp))
        return result

    def _read_radios(self, radios):
        if not self.batch_radio_reads:
            return {radio: self._radio_metrics(radio) for radio in radios}
        values = self._call(self.NETWORK, "_get",
                            {"rel_path": "Device.*.Radio.*.UnassociatedSTA.", "depth": 2})
        return {
            radio: self._parse_radio_metrics({
                key: value for key, value in values.items()
                if key.startswith(radio + ".UnassociatedSTA.")
            }) for radio in radios
        }

    def _await_timestamp_resolution(self, baseline):
        timestamps = [parse_time(value[1]) for value in baseline.values()]
        if not timestamps:
            return
        latest = max(timestamps)
        if latest.microsecond:
            return
        remaining = latest.timestamp() + 1 - time.time()
        if not 0 < remaining <= 1:
            return
        started = time.monotonic()
        deadline = started + remaining
        while time.monotonic() < deadline:
            if self.generation_guard is not None and not self.generation_guard():
                raise CandidateSnapshotSuperseded("prplMesh publication admission was superseded")
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        self.last_raw.append({
            "operation": "timestamp_resolution_wait", "resolution_ms": 1000,
            "baseline_timestamp": latest.isoformat(),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
        })

    def _register_targets(self, targets, radio_metadata):
        channels = {}
        for radio, sta_mac in targets:
            meta = radio_metadata[radio]
            channels.setdefault(sta_mac, set()).add((int(meta["channel"]), int(meta["opclass"])))
        changed = {station for station, values in channels.items()
                   if self.registration_channels.get(station) != frozenset(values)}
        self.registered = {key for key in self.registered if key[1] not in changed}
        self.registration_channels.update({station: frozenset(values) for station, values in channels.items()})
        pending = sorted(set(targets) - self.registered)
        if not pending:
            return
        failure = None
        with ThreadPoolExecutor(max_workers=min(4, len(pending)),
                                thread_name_prefix="prpl-candidate-registration") as executor:
            requests = {}
            for radio, sta_mac in pending:
                meta = radio_metadata[radio]
                request = {
                    "un_station_mac": sta_mac,
                    "channel": int(meta["channel"]),
                    "operating_class": int(meta["opclass"]),
                    "agent_mac": meta["device_id"],
                }
                if self.defer_registration_query:
                    request["defer_query"] = True
                future = executor.submit(self._call, radio, "AddUnassociatedStation", request)
                requests[future] = (radio, sta_mac, request)
            for future in as_completed(requests):
                if future.cancelled():
                    continue
                radio, sta_mac, request = requests[future]
                try:
                    response = future.result()
                except Exception as error:
                    failure = failure or error
                    for outstanding in requests:
                        outstanding.cancel()
                else:
                    self.last_raw.append({"operation": "register", "radio": radio,
                                          "request": request, "response": response})
                    self.registered.add((radio, sta_mac))
        if failure is not None:
            raise failure

    def __call__(
        self,
        clients: tuple[ClientObservation, ...],
        inventory: tuple[CandidateObservation, ...],
        bsses: list[dict[str, Any]],
        observed_at: str,
    ) -> Iterable[CandidateObservation]:
        if not self.allow_simulated:
            raise CandidateMetricsError(
                "prplMesh candidate provider is hwsim/wmediumd-backed; "
                "use --allow-simulated-candidates in this lab"
            )
        self.last_raw = []
        collection_started = time.monotonic()
        clients_by_mac = {item.sta_mac: item for item in clients}
        bss_by_id = {item["bssid"]: item for item in bsses}
        radio_objects = self._radio_objects()
        targets: dict[tuple[str, str], list[CandidateObservation]] = {}
        radio_metadata: dict[str, dict[str, Any]] = {}
        candidates = sorted(inventory, key=lambda candidate: (
            not bool(self.client_prioritizer and candidate.sta_mac in clients_by_mac
                     and self.client_prioritizer(clients_by_mac[candidate.sta_mac], observed_at)),
            candidate.sta_mac, candidate.bssid,
        ))
        for candidate in candidates:
            client = clients_by_mac.get(candidate.sta_mac)
            bss = bss_by_id.get(candidate.bssid)
            if client is None or bss is None or candidate.band != client.band:
                continue
            if self.client_selector is not None and not self.client_selector(
                client, observed_at
            ):
                continue
            key = (bss["device_id"], bss["radio_id"])
            radio = radio_objects.get(key)
            if radio is None:
                self.object_cache.clear()
                self.registered.clear()
                raise CandidateMetricsUnavailable(f"no NBAPI radio object for {key}; rediscovery required")
            targets.setdefault((radio, candidate.sta_mac), []).append(candidate)
            radio_metadata[radio] = bss

        self.last_selected_sta_macs = {sta_mac for _, sta_mac in targets}
        self.last_requested_sta_macs = set(self.last_selected_sta_macs)
        self.last_selection = {"eligible_clients": len(clients),
                               "selected_clients": len(self.last_selected_sta_macs),
                               "native_registration_deferred": self.defer_registration_query,
                               "backend": "prplmesh_nbapi"}
        if not targets:
            return []
        self._register_targets(targets, radio_metadata)

        radios = sorted({radio for radio, _ in targets})
        baseline = {
            (radio, sta_mac): value
            for radio, values in self._read_radios(radios).items()
            for sta_mac, value in values.items()
        }
        self._await_timestamp_resolution(baseline)
        response = self._call(self.NETWORK, "UpdateUnassociatedStationsStats", {})
        self.last_raw.append({"operation": "update", "response": response})
        self.progress({"phase": "collecting", "selected_clients": len(self.last_selected_sta_macs),
                       "candidate_comparisons": sum(len(items) for items in targets.values())})
        deadline = time.monotonic() + self.timeout_seconds
        retry_delay = 2.0
        retry_at = time.monotonic() + retry_delay
        retries = 0
        expected = set(targets)
        metrics: dict[tuple[str, str], tuple[int, str]] = {}
        published = set()
        measured = []
        last_read = {}
        last_read_at = None
        while time.monotonic() < deadline:
            if self.generation_guard is not None and not self.generation_guard():
                raise CandidateSnapshotSuperseded("prplMesh candidate generation was superseded")
            last_read = self._read_radios(radios)
            last_read_at = format_time(datetime.now(timezone.utc))
            for radio, values in last_read.items():
                for sta_mac, value in values.items():
                    key = (radio, sta_mac)
                    if key not in baseline or parse_time(value[1]) > parse_time(baseline[key][1]):
                        metrics[key] = value
                ready = {key for key in expected if key[0] == radio and key in metrics} - published
                batch = [replace(candidate, rcpi=metrics[key][0], metric_observed_at=metrics[key][1],
                                 measurement_source="easy_mesh_unassociated_sta_link_metrics:prplmesh-nl80211-hwsim-wmediumd:simulated")
                         for key in sorted(ready) for candidate in targets[key]]
                if batch:
                    transaction = {"operation": "published", "radio": radio,
                                   "measurements": len(batch), "finished_at": format_time(datetime.now(timezone.utc)),
                                   "elapsed_ms": round((time.monotonic() - collection_started) * 1000, 3)}
                    measured.extend(batch)
                    published.update(ready)
                    self.last_raw.append(transaction)
                    if self.result_ready:
                        self.result_ready(batch, set(), transaction)
            if expected.issubset(metrics):
                break
            if retries < 3 and retry_at <= time.monotonic() < deadline:
                response = self._call(self.NETWORK, "UpdateUnassociatedStationsStats", {})
                retries += 1
                self.last_raw.append({"operation": "update_retry", "attempt": retries,
                                      "missing": sorted(expected - set(metrics)),
                                      "response": response,
                                      "elapsed_ms": round((time.monotonic() - collection_started) * 1000, 3)})
                retry_delay *= 2
                retry_at = time.monotonic() + retry_delay
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        missing = sorted(expected - set(metrics))
        if missing:
            self.last_raw.append({"operation": "incomplete", "missing": sorted(missing),
                                  "last_read_at": last_read_at,
                                  "freshness": [{"radio": radio, "sta_mac": sta_mac,
                                                 "baseline": baseline.get((radio, sta_mac)),
                                                 "last_read": last_read.get(radio, {}).get(sta_mac)}
                                                for radio, sta_mac in missing],
                                  "elapsed_ms": round((time.monotonic() - collection_started) * 1000, 3)})
            raise CandidateMetricsUnavailable(
                f"prplMesh candidate metrics incomplete after {self.timeout_seconds}s: {missing}"
            )

        self.last_raw.append(
            {"operation": "complete", "measurements": len(measured),
             "elapsed_ms": round((time.monotonic() - collection_started) * 1000, 3)}
        )
        return measured
