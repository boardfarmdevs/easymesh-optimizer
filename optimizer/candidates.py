from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import time
from typing import Any, Callable, Iterable
import urllib.error
import urllib.request

from .model import (
    CandidateObservation,
    ClientObservation,
    format_time,
    normalize_band,
    normalize_mac,
)


JsonRequester = Callable[[str, dict[str, Any]], dict[str, Any]]

# The hwsim lab deliberately uses one fixed 20 MHz control channel per band.
# The controller's current model exposes Band but leaves Radio.Channel at zero;
# this mapping is therefore only legal behind the explicit simulated-provider
# opt-in.  A physical deployment must report its actual operating channel.
LAB_CONTROL_CHANNELS = {"2.4": 6, "5": 36, "6": 5}

# The unified-wifi-mesh data model stores at most EM_MAX_UNASSOC_STA (eight)
# response entries.  Sending a larger request makes the current Agent omit a
# correlated response, so keep every transaction within that real boundary.
MAX_UNASSOC_STAS_PER_QUERY = 8

# The HTTP adapter serializes every native libemcli call.  Agent protocol
# transactions are independent, but concurrent HTTP handlers still contend on
# that single command path while each handler polls through the same adapter.
# Keep the release collector serial at this boundary.  A future adapter with a
# genuinely concurrent command/result API can opt in to more workers.
DEFAULT_MAX_PARALLEL_AGENTS = 1
HTTP_REQUEST_TIMEOUT_SECONDS = 20


class CandidateMetricsError(RuntimeError):
    """The controller could not produce a trustworthy candidate snapshot."""


def operating_class(band: str | int, channel: int) -> int:
    """Return the 20 MHz global operating class used by the lab radio."""
    normalized = normalize_band(band)
    if normalized == "2.4" and 1 <= channel <= 13:
        return 81
    if normalized == "5":
        if 36 <= channel <= 48:
            return 115
        if 52 <= channel <= 64:
            return 118
        if 100 <= channel <= 144:
            return 121
        if 149 <= channel <= 161:
            return 124
        if channel in (165, 169, 173, 177):
            return 125
    if normalized == "6" and 1 <= channel <= 233:
        return 131
    raise CandidateMetricsError(
        f"no supported 20 MHz operating class for band={normalized} channel={channel}"
    )


def _default_request(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=HTTP_REQUEST_TIMEOUT_SECONDS
        ) as response:  # nosec B310
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            detail = json.load(error)
        except (json.JSONDecodeError, UnicodeDecodeError):
            detail = {"message": error.read().decode(errors="replace")}
        raise CandidateMetricsError(
            f"candidate query failed with HTTP {error.code}: {detail}"
        ) from error
    except (OSError, TimeoutError, json.JSONDecodeError) as error:
        raise CandidateMetricsError(f"candidate query failed: {error}") from error


def _metric_time(milliseconds: Any) -> str:
    try:
        value = int(milliseconds)
    except (TypeError, ValueError) as error:
        raise CandidateMetricsError("candidate metric has no receipt timestamp") from error
    if value <= 0:
        raise CandidateMetricsError("candidate metric has an invalid receipt timestamp")
    return format_time(datetime.fromtimestamp(value / 1000, tz=timezone.utc))


class ControllerCandidateProvider:
    """Collect active EasyMesh candidate measurements and map them to BSSIDs."""

    def __init__(
        self,
        base_url: str,
        *,
        requester: JsonRequester | None = None,
        allow_simulated: bool = False,
        max_parallel_agents: int = DEFAULT_MAX_PARALLEL_AGENTS,
        request_attempts: int = 1,
        retry_delay_seconds: float = 0.25,
    ) -> None:
        if max_parallel_agents < 1:
            raise ValueError("max_parallel_agents must be positive")
        if request_attempts < 1:
            raise ValueError("request_attempts must be positive")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must not be negative")
        self.url = base_url.rstrip("/") + "/api/v1/unassoc_sta_query"
        self.requester = requester or _default_request
        self.allow_simulated = allow_simulated
        self.max_parallel_agents = max_parallel_agents
        self.request_attempts = request_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.last_raw: list[dict[str, Any]] = []
        self.last_rejected_candidate_keys: set[tuple[str, str]] = set()

    def _channel(self, raw: dict[str, Any]) -> int:
        channel = int(raw.get("channel") or 0)
        if channel > 0:
            return channel
        band = normalize_band(raw.get("band"))
        if self.allow_simulated and band in LAB_CONTROL_CHANNELS:
            return LAB_CONTROL_CHANNELS[band]
        raise CandidateMetricsError(
            f"BSS {raw.get('bssid', '<unknown>')} has no operating channel; "
            "the controller must report it for a physical candidate query"
        )

    def __call__(
        self,
        clients: tuple[ClientObservation, ...],
        inventory: tuple[CandidateObservation, ...],
        bsses: list[dict[str, Any]],
        observed_at: str,
    ) -> Iterable[CandidateObservation]:
        del observed_at  # individual controller receipt times are authoritative
        self.last_rejected_candidate_keys = set()
        clients_by_mac = {item.sta_mac: item for item in clients}
        bss_by_id = {
            normalize_mac(item["bssid"]): item
            for item in bsses
            if item.get("bssid")
        }
        targets: dict[
            tuple[str, str, str, int, int], list[CandidateObservation]
        ] = {}
        query_groups: dict[
            tuple[str, str], dict[tuple[int, int], set[str]]
        ] = {}

        for candidate in inventory:
            raw = bss_by_id.get(candidate.bssid)
            client = clients_by_mac.get(candidate.sta_mac)
            if raw is None or client is None:
                continue
            # An Unassociated STA Link Metrics Query asks a candidate radio to
            # hear the STA on the channel where it is currently transmitting.
            # It is a same-band candidate primitive, not evidence that an
            # associated STA would work on a different band.  Cross-band
            # policy needs beacon/probe/capability observations instead.
            if candidate.band != client.band:
                continue
            agent = normalize_mac(raw["device_id"])
            radio = normalize_mac(raw["radio_id"])
            channel = self._channel(raw)
            opclass = operating_class(raw.get("band"), channel)
            targets.setdefault(
                (agent, radio, candidate.sta_mac, opclass, channel), []
            ).append(candidate)
            # OneWifi routes GetNaSta through one VAP.  A single EasyMesh query
            # must therefore remain on one agent radio; combining opclasses
            # from different radios is rejected by the agent.
            query_groups.setdefault((agent, radio), {}).setdefault(
                (opclass, channel), set()
            ).add(candidate.sta_mac)

        jobs_by_agent: dict[
            str, list[tuple[str, list[tuple[int, int, str]]]]
        ] = {}
        for (agent, query_radio), groups in sorted(query_groups.items()):
            entries = [
                (opclass, channel, station)
                for (opclass, channel), stations in sorted(groups.items())
                for station in sorted(stations)
            ]
            batches = [
                entries[offset:offset + MAX_UNASSOC_STAS_PER_QUERY]
                for offset in range(0, len(entries), MAX_UNASSOC_STAS_PER_QUERY)
            ]
            jobs_by_agent.setdefault(agent, []).extend(
                (query_radio, batch) for batch in batches
            )

        # Each Agent owns an independent unassociated-STA response table and
        # EasyMesh command path.  Keep batches for one Agent sequential so a
        # later response cannot replace the previous eight-entry table before
        # it is consumed.  The current HTTP adapter also serializes native
        # libemcli execution globally, so the release default runs Agents one
        # at a time instead of creating competing polling handlers.
        transactions_by_agent: dict[str, list[dict[str, Any]]] = {
            agent: [] for agent in jobs_by_agent
        }

        def query_agent(
            agent: str,
            jobs: list[tuple[str, list[tuple[int, int, str]]]],
        ) -> tuple[list[CandidateObservation], set[tuple[str, str]]]:
            agent_measured: list[CandidateObservation] = []
            agent_rejected: set[tuple[str, str]] = set()
            for query_radio, batch in jobs:
                by_opclass: dict[int, dict[int, list[str]]] = {}
                for opclass, channel, station in batch:
                    by_opclass.setdefault(opclass, {}).setdefault(channel, []).append(
                        station
                    )
                payload = {
                    "AlMac": agent,
                    "UnassocStaQueryList": [
                        {
                            "opclass": opclass,
                            "channels": [
                                {"channel": channel, "sta_macs": stations}
                                for channel, stations in sorted(channels.items())
                            ],
                        }
                        for opclass, channels in sorted(by_opclass.items())
                    ],
                }
                response = None
                for attempt in range(1, self.request_attempts + 1):
                    transaction = {
                        "request": payload,
                        "query_radio": query_radio,
                    }
                    if self.request_attempts > 1:
                        transaction["attempt"] = attempt
                    transactions_by_agent[agent].append(transaction)
                    try:
                        response = self.requester(self.url, payload)
                        break
                    except CandidateMetricsError as error:
                        transaction["error"] = str(error)
                        if attempt == self.request_attempts:
                            suffix = (
                                f": {error}" if self.request_attempts == 1
                                else f" after {attempt} attempt(s): {error}"
                            )
                            raise CandidateMetricsError(
                                f"candidate query failed for agent {agent} radio "
                                f"{query_radio}{suffix}"
                            ) from error
                        time.sleep(self.retry_delay_seconds)
                assert response is not None
                transaction["response"] = response
                if response.get("success") is not True:
                    raise CandidateMetricsError(
                        f"candidate query for {agent} was not successful: {response}"
                    )
                if response.get("simulated") is True and not self.allow_simulated:
                    raise CandidateMetricsError(
                        "controller returned simulated candidate metrics; "
                        "use --allow-simulated-candidates only in the hwsim lab"
                    )
                provider = str(response.get("provider") or "unknown")
                simulated = response.get("simulated") is True
                source = f"easy_mesh_unassociated_sta_link_metrics:{provider}"
                if simulated:
                    source += ":simulated"

                expected = {
                    (agent, query_radio, station, opclass, channel)
                    for opclass, channel, station in batch
                }
                received: set[tuple[str, str, str, int, int]] = set()
                # Successful controller replies always define both list-valued
                # fields, but older helpers encoded an empty Go slice as JSON
                # null.  Treat absent and null as the same empty compatibility
                # value while retaining strict validation for list entries.
                for metric in response.get("metrics") or []:
                    metric_agent = normalize_mac(metric["agent_al"])
                    radio = normalize_mac(metric["ruid"])
                    sta = normalize_mac(metric["sta"])
                    opclass = int(metric["opclass"])
                    channel = int(metric["channel"])
                    rcpi = int(metric["rcpi"])
                    if metric_agent != agent:
                        raise CandidateMetricsError(
                            f"candidate response agent {metric_agent} does not match {agent}"
                        )
                    if not 0 <= rcpi <= 220:
                        raise CandidateMetricsError(f"candidate RCPI is invalid: {rcpi}")
                    response_key = (metric_agent, radio, sta, opclass, channel)
                    if response_key not in expected:
                        raise CandidateMetricsError(
                            "candidate response contains an unexpected measurement: "
                            f"{sta}@{radio} opclass={opclass} channel={channel}"
                        )
                    received.add(response_key)
                    mapped = targets.get(response_key, [])
                    for candidate in mapped:
                        agent_measured.append(
                            CandidateObservation(
                                sta_mac=sta,
                                bssid=candidate.bssid,
                                device_id=candidate.device_id,
                                device_name=candidate.device_name,
                                rcpi=rcpi,
                                metric_observed_at=_metric_time(
                                    metric.get("received_at_ms")
                                ),
                                measurement_source=source,
                                band=candidate.band,
                            )
                        )
                rejected: set[tuple[str, str, str, int, int]] = set()
                for candidate_error in response.get("rejected") or []:
                    error_agent = normalize_mac(candidate_error["agent_al"])
                    radio = normalize_mac(candidate_error["ruid"])
                    sta = normalize_mac(candidate_error["sta"])
                    error_code = int(candidate_error["error_code"])
                    if error_agent != agent:
                        raise CandidateMetricsError(
                            f"candidate rejection agent {error_agent} does not match {agent}"
                        )
                    if error_code <= 0:
                        raise CandidateMetricsError(
                            f"candidate rejection has invalid error code: {error_code}"
                        )
                    _metric_time(candidate_error.get("received_at_ms"))
                    matched = {
                        key for key in expected
                        if key[0] == error_agent and key[1] == radio and key[2] == sta
                    }
                    if not matched:
                        raise CandidateMetricsError(
                            "candidate response contains an unexpected rejection: "
                            f"{sta}@{radio} error={error_code}"
                        )
                    if matched & received:
                        raise CandidateMetricsError(
                            f"candidate response both measured and rejected {sta}@{radio}"
                        )
                    rejected.update(matched)
                    for response_key in matched:
                        for candidate in targets.get(response_key, []):
                            agent_rejected.add((candidate.sta_mac, candidate.bssid))
                missing = sorted(expected - received - rejected)
                if missing:
                    missing_text = ", ".join(
                        f"{sta}@{radio}/opclass-{opclass}/channel-{channel}"
                        for _agent, radio, sta, opclass, channel in missing
                    )
                    raise CandidateMetricsError(
                        f"candidate response for agent {agent} radio {query_radio} "
                        f"omitted {missing_text}"
                    )
            return agent_measured, agent_rejected

        measured_by_agent: dict[str, list[CandidateObservation]] = {}
        rejected_by_agent: dict[str, set[tuple[str, str]]] = {}
        failures: dict[str, CandidateMetricsError] = {}
        if jobs_by_agent:
            with ThreadPoolExecutor(
                max_workers=min(self.max_parallel_agents, len(jobs_by_agent)),
                thread_name_prefix="candidate-agent",
            ) as executor:
                future_agents = {
                    executor.submit(query_agent, agent, jobs): agent
                    for agent, jobs in jobs_by_agent.items()
                }
                for future in as_completed(future_agents):
                    agent = future_agents[future]
                    try:
                        measured, rejected = future.result()
                        measured_by_agent[agent] = measured
                        rejected_by_agent[agent] = rejected
                    except CandidateMetricsError as error:
                        failures[agent] = error

        self.last_raw = [
            transaction
            for agent in sorted(transactions_by_agent)
            for transaction in transactions_by_agent[agent]
        ]
        if failures:
            raise failures[sorted(failures)[0]]

        self.last_rejected_candidate_keys = {
            key
            for agent in sorted(rejected_by_agent)
            for key in rejected_by_agent[agent]
        }

        measured = [
            item
            for agent in sorted(measured_by_agent)
            for item in measured_by_agent[agent]
        ]
        return measured
