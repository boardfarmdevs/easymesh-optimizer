from __future__ import annotations

from dataclasses import replace
from threading import Barrier, Lock

from optimizer.candidates import (
    CandidateMetricsError,
    ControllerCandidateProvider,
    operating_class,
)
from optimizer.model import CandidateObservation, ClientObservation
import pytest


STA = "02:00:00:00:03:00"
AGENT = "02:00:00:00:09:20"
RADIO = "02:00:00:00:09:00"
BSSID = "02:00:00:aa:aa:01"


def client() -> ClientObservation:
    return ClientObservation(
        sta_mac=STA,
        connected_device_id="02:00:00:00:08:20",
        connected_device_name="Extender-2",
        connected_bssid="02:00:00:bb:bb:01",
        rcpi=80,
        association_uptime_seconds=120,
        metric_observed_at="2026-08-21T20:00:00.000Z",
        measurement_source="associated_sta_link_metrics",
        band="5",
    )


def inventory() -> CandidateObservation:
    return CandidateObservation(
        sta_mac=STA,
        bssid=BSSID,
        device_id=AGENT,
        device_name="Extender-1",
        rcpi=None,
        metric_observed_at=None,
        measurement_source="controller_bss_inventory_only",
        band="5",
    )


def bsses():
    return [{
        "bssid": BSSID,
        "device_id": AGENT,
        "radio_id": RADIO,
        "band": 1,
        "channel": 36,
        "ssid": "private_ssid",
        "haul_type": "Fronthaul",
    }]


def response(simulated=True):
    return {
        "success": True,
        "provider": "hwsim-wmediumd-read-only",
        "simulated": simulated,
        "metrics": [{
            "agent_al": AGENT,
            "ruid": RADIO,
            "sta": STA,
            "opclass": 115,
            "channel": 36,
            "rcpi": 136,
            "received_at_ms": 1787342400123,
            "message_id": 42,
        }],
    }


def test_lab_operating_class_mapping_is_frequency_qualified():
    assert operating_class("2.4", 6) == 81
    assert operating_class("5", 36) == 115
    assert operating_class("5", 100) == 121
    assert operating_class("6", 5) == 131


def test_provider_batches_query_and_maps_ruid_to_exact_bssid():
    calls = []

    def request(url, payload):
        calls.append((url, payload))
        return response()

    provider = ControllerCandidateProvider(
        "http://controller", requester=request, allow_simulated=True
    )
    measured = list(provider(
        (client(),), (inventory(),), bsses(), "2026-08-21T20:00:01.000Z"
    ))
    assert calls == [(
        "http://controller/api/v1/unassoc_sta_query",
        {
            "AlMac": AGENT,
            "UnassocStaQueryList": [{
                "opclass": 115,
                "channels": [{"channel": 36, "sta_macs": [STA]}],
            }],
        },
    )]
    assert len(measured) == 1
    assert measured[0].bssid == BSSID
    assert measured[0].rcpi == 136
    assert measured[0].metric_observed_at == "2026-08-21T20:00:00.123Z"
    assert measured[0].measurement_source.endswith(":simulated")
    assert provider.last_raw[0]["response"]["metrics"][0]["message_id"] == 42
    assert provider.last_selected_sta_macs == {STA}


def test_provider_queries_only_policy_selected_clients():
    second_sta = "02:00:00:00:04:00"
    calls = []

    def request(_url, payload):
        calls.append(payload)
        selected_sta = payload["UnassocStaQueryList"][0]["channels"][0][
            "sta_macs"
        ][0]
        result = response()
        result["metrics"][0]["sta"] = selected_sta
        return result

    provider = ControllerCandidateProvider(
        "http://controller",
        requester=request,
        allow_simulated=True,
        client_selector=lambda item, _observed_at: item.sta_mac == second_sta,
    )
    measured = list(provider(
        (client(), replace(client(), sta_mac=second_sta)),
        (inventory(), replace(inventory(), sta_mac=second_sta)),
        bsses(),
        "2026-08-21T20:00:01.000Z",
    ))

    assert [item.sta_mac for item in measured] == [second_sta]
    assert calls[0]["UnassocStaQueryList"][0]["channels"][0]["sta_macs"] == [
        second_sta
    ]
    assert provider.last_selected_sta_macs == {second_sta}


def test_provider_policy_selection_can_skip_all_active_queries():
    calls = []
    provider = ControllerCandidateProvider(
        "http://controller",
        requester=lambda _url, payload: calls.append(payload) or response(),
        allow_simulated=True,
        client_selector=lambda _item, _observed_at: False,
    )

    assert list(provider(
        (client(),), (inventory(),), bsses(), "2026-08-21T20:00:01.000Z"
    )) == []
    assert calls == []
    assert provider.last_raw == []
    assert provider.last_selected_sta_macs == set()


@pytest.mark.parametrize(
    ("metrics_value", "rejected_value"),
    [
        ("absent", "absent"),
        (None, None),
        ([], []),
    ],
    ids=("absent", "null", "empty"),
)
def test_optional_candidate_result_lists_accept_empty_wire_representations(
    metrics_value, rejected_value
):
    result = response()
    if metrics_value == "absent":
        result.pop("metrics")
    else:
        result["metrics"] = metrics_value
    if rejected_value != "absent":
        result["rejected"] = rejected_value

    provider = ControllerCandidateProvider(
        "http://controller", requester=lambda _url, _payload: result,
        allow_simulated=True,
    )
    with pytest.raises(CandidateMetricsError, match=f"omitted {STA}"):
        list(provider(
            (client(),), (inventory(),), bsses(),
            "2026-08-21T20:00:01.000Z",
        ))


def test_measured_response_accepts_legacy_null_rejection_list():
    result = response()
    result["rejected"] = None
    provider = ControllerCandidateProvider(
        "http://controller", requester=lambda _url, _payload: result,
        allow_simulated=True,
    )

    measured = list(provider(
        (client(),), (inventory(),), bsses(),
        "2026-08-21T20:00:01.000Z",
    ))

    assert [item.sta_mac for item in measured] == [STA]
    assert provider.last_rejected_candidate_keys == set()


def test_all_rejected_response_accepts_legacy_null_metric_list():
    result = response()
    result["metrics"] = None
    result["rejected"] = [{
        "agent_al": AGENT,
        "ruid": RADIO,
        "sta": STA,
        "error_code": 1,
        "received_at_ms": 1787342400124,
        "message_id": 42,
    }]
    provider = ControllerCandidateProvider(
        "http://controller", requester=lambda _url, _payload: result,
        allow_simulated=True,
    )

    measured = list(provider(
        (client(),), (inventory(),), bsses(),
        "2026-08-21T20:00:01.000Z",
    ))

    assert measured == []
    assert provider.last_rejected_candidate_keys == {(STA, BSSID)}


def test_simulated_candidate_source_requires_explicit_lab_opt_in():
    provider = ControllerCandidateProvider(
        "http://controller", requester=lambda _url, _payload: response()
    )
    try:
        list(provider(
            (client(),), (inventory(),), bsses(), "2026-08-21T20:00:01.000Z"
        ))
    except CandidateMetricsError as error:
        assert "--allow-simulated-candidates" in str(error)
    else:
        raise AssertionError("simulated metrics were accepted without opt-in")


def test_simulated_lab_uses_frozen_channel_plan_when_controller_channel_is_zero():
    raw = bsses()
    raw[0].pop("channel")
    calls = []
    provider = ControllerCandidateProvider(
        "http://controller",
        requester=lambda _url, payload: calls.append(payload) or response(),
        allow_simulated=True,
    )
    measured = list(provider(
        (client(),), (inventory(),), raw, "2026-08-21T20:00:01.000Z"
    ))
    assert len(measured) == 1
    assert calls[0]["UnassocStaQueryList"][0] == {
        "opclass": 115,
        "channels": [{"channel": 36, "sta_macs": [STA]}],
    }


def test_missing_physical_operating_channel_is_not_invented():
    raw = bsses()
    raw[0]["channel"] = 0
    provider = ControllerCandidateProvider(
        "http://controller", requester=lambda _url, _payload: response()
    )
    try:
        list(provider(
            (client(),), (inventory(),), raw, "2026-08-21T20:00:01.000Z"
        ))
    except CandidateMetricsError as error:
        assert "must report it for a physical candidate query" in str(error)
    else:
        raise AssertionError("a physical BSS with no channel was queried")


def test_queries_for_two_radios_are_not_combined():
    second_radio = "02:00:00:00:09:01"
    second_bssid = "02:00:00:aa:aa:02"
    second_inventory = CandidateObservation(
        sta_mac=STA,
        bssid=second_bssid,
        device_id=AGENT,
        device_name="Extender-1",
        rcpi=None,
        metric_observed_at=None,
        measurement_source="controller_bss_inventory_only",
        band="5",
    )
    raw = bsses() + [{
        "bssid": second_bssid,
        "device_id": AGENT,
        "radio_id": second_radio,
        "band": 1,
        "channel": 40,
        "ssid": "private_ssid",
        "haul_type": "Fronthaul",
    }]
    calls = []

    def request(_url, payload):
        calls.append(payload)
        op = payload["UnassocStaQueryList"][0]
        channel = op["channels"][0]["channel"]
        radio = RADIO if channel == 36 else second_radio
        result = response()
        result["metrics"][0].update({
            "ruid": radio,
            "opclass": op["opclass"],
            "channel": op["channels"][0]["channel"],
        })
        return result

    provider = ControllerCandidateProvider(
        "http://controller", requester=request, allow_simulated=True
    )
    measured = list(provider(
        (client(),), (inventory(), second_inventory), raw,
        "2026-08-21T20:00:01.000Z",
    ))
    assert len(calls) == 2
    assert all(len(call["UnassocStaQueryList"]) == 1 for call in calls)
    assert {item.bssid for item in measured} == {BSSID, second_bssid}


def test_different_agents_are_queried_concurrently_with_stable_results():
    second_agent = "02:00:00:00:0a:20"
    second_radio = "02:00:00:00:0a:00"
    second_bssid = "02:00:00:cc:cc:01"
    second_inventory = replace(
        inventory(),
        bssid=second_bssid,
        device_id=second_agent,
        device_name="Extender-2",
    )
    raw = bsses() + [{
        "bssid": second_bssid,
        "device_id": second_agent,
        "radio_id": second_radio,
        "band": 1,
        "channel": 36,
        "ssid": "private_ssid",
        "haul_type": "Fronthaul",
    }]
    barrier = Barrier(2, timeout=2)
    calls = []
    calls_lock = Lock()

    def request(_url, payload):
        agent = payload["AlMac"]
        with calls_lock:
            calls.append(agent)
        barrier.wait()
        result = response()
        result["metrics"][0].update({
            "agent_al": agent,
            "ruid": RADIO if agent == AGENT else second_radio,
        })
        return result

    provider = ControllerCandidateProvider(
        "http://controller", requester=request, allow_simulated=True,
        max_parallel_agents=2,
    )
    measured = list(provider(
        (client(),), (inventory(), second_inventory), raw,
        "2026-08-21T20:00:01.000Z",
    ))
    assert set(calls) == {AGENT, second_agent}
    assert [item.bssid for item in measured] == [BSSID, second_bssid]
    assert [item["request"]["AlMac"] for item in provider.last_raw] == [
        AGENT, second_agent,
    ]


def test_default_serializes_agents_for_the_native_cli_adapter():
    second_agent = "02:00:00:00:0a:20"
    second_radio = "02:00:00:00:0a:00"
    second_bssid = "02:00:00:cc:cc:01"
    second_inventory = replace(
        inventory(), bssid=second_bssid, device_id=second_agent,
        device_name="Extender-2",
    )
    raw = bsses() + [{
        "bssid": second_bssid,
        "device_id": second_agent,
        "radio_id": second_radio,
        "band": 1,
        "channel": 36,
        "ssid": "private_ssid",
        "haul_type": "Fronthaul",
    }]
    active = 0
    maximum_active = 0
    calls_lock = Lock()

    def request(_url, payload):
        nonlocal active, maximum_active
        with calls_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        result = response()
        agent = payload["AlMac"]
        result["metrics"][0].update({
            "agent_al": agent,
            "ruid": RADIO if agent == AGENT else second_radio,
        })
        with calls_lock:
            active -= 1
        return result

    provider = ControllerCandidateProvider(
        "http://controller", requester=request, allow_simulated=True
    )
    measured = list(provider(
        (client(),), (inventory(), second_inventory), raw,
        "2026-08-21T20:00:01.000Z",
    ))
    assert maximum_active == 1
    assert [item.bssid for item in measured] == [BSSID, second_bssid]


def test_provider_splits_requests_at_controller_eight_sta_limit():
    clients = []
    candidates = []
    for index in range(9):
        station = f"02:00:00:00:{index + 16:02x}:00"
        clients.append(replace(client(), sta_mac=station))
        candidates.append(replace(inventory(), sta_mac=station))

    calls = []

    def request(_url, payload):
        calls.append(payload)
        stations = [
            station
            for opclass in payload["UnassocStaQueryList"]
            for channel in opclass["channels"]
            for station in channel["sta_macs"]
        ]
        result = response()
        result["metrics"] = [
            {**result["metrics"][0], "sta": station}
            for station in stations
        ]
        return result

    provider = ControllerCandidateProvider(
        "http://controller", requester=request, allow_simulated=True
    )
    measured = list(provider(
        tuple(clients), tuple(candidates), bsses(),
        "2026-08-21T20:00:01.000Z",
    ))
    batch_sizes = [
        sum(
            len(channel["sta_macs"])
            for opclass in call["UnassocStaQueryList"]
            for channel in opclass["channels"]
        )
        for call in calls
    ]
    assert batch_sizes == [8, 1]
    assert len(measured) == 9


def test_cross_band_inventory_is_not_misreported_as_candidate_measurement():
    raw = bsses()
    raw[0]["band"] = 3
    raw[0]["channel"] = 5
    cross_band = CandidateObservation(
        sta_mac=STA,
        bssid=BSSID,
        device_id=AGENT,
        device_name="Extender-1",
        rcpi=None,
        metric_observed_at=None,
        measurement_source="controller_bss_inventory_only",
        band="6",
    )
    calls = []
    provider = ControllerCandidateProvider(
        "http://controller",
        requester=lambda _url, payload: calls.append(payload) or response(),
        allow_simulated=True,
    )
    assert list(provider(
        (client(),), (cross_band,), raw, "2026-08-21T20:00:01.000Z"
    )) == []
    assert calls == []


def test_failed_query_records_agent_radio_and_failed_transaction():
    def fail(_url, _payload):
        raise CandidateMetricsError("HTTP 504")

    provider = ControllerCandidateProvider(
        "http://controller", requester=fail, allow_simulated=True
    )
    with pytest.raises(
        CandidateMetricsError,
        match=f"agent {AGENT} radio {RADIO}: HTTP 504",
    ):
        list(provider(
            (client(),), (inventory(),), bsses(), "2026-08-21T20:00:01.000Z"
        ))
    assert provider.last_raw == [{
        "request": {
            "AlMac": AGENT,
            "UnassocStaQueryList": [{
                "opclass": 115,
                "channels": [{"channel": 36, "sta_macs": [STA]}],
            }],
        },
        "query_radio": RADIO,
        "error": "HTTP 504",
    }]


def test_idempotent_candidate_query_can_retry_one_transport_timeout():
    calls = 0

    def transient(_url, _payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise CandidateMetricsError("HTTP 504")
        return response()

    provider = ControllerCandidateProvider(
        "http://controller", requester=transient, allow_simulated=True,
        request_attempts=2, retry_delay_seconds=0,
    )
    measured = list(provider(
        (client(),), (inventory(),), bsses(), "2026-08-21T20:00:01.000Z"
    ))
    assert len(measured) == 1
    assert calls == 2
    assert provider.last_raw[0]["attempt"] == 1
    assert provider.last_raw[0]["error"] == "HTTP 504"
    assert provider.last_raw[1]["attempt"] == 2
    assert provider.last_raw[1]["response"]["success"] is True


def test_partial_success_response_is_rejected():
    second_sta = "02:00:00:00:04:00"
    clients = (client(), replace(client(), sta_mac=second_sta))
    candidates = (inventory(), replace(inventory(), sta_mac=second_sta))

    provider = ControllerCandidateProvider(
        "http://controller", requester=lambda _url, _payload: response(),
        allow_simulated=True,
    )

    with pytest.raises(CandidateMetricsError, match=f"omitted {second_sta}"):
        list(provider(
            clients, candidates, bsses(), "2026-08-21T20:00:01.000Z"
        ))


def test_explicit_partial_rejection_completes_without_inventing_rcpi():
    second_sta = "02:00:00:00:04:00"
    clients = (client(), replace(client(), sta_mac=second_sta))
    candidates = (inventory(), replace(inventory(), sta_mac=second_sta))
    result = response()
    result["rejected"] = [{
        "agent_al": AGENT,
        "ruid": RADIO,
        "sta": second_sta,
        "error_code": 1,
        "received_at_ms": 1787342400124,
        "message_id": 42,
    }]

    provider = ControllerCandidateProvider(
        "http://controller", requester=lambda _url, _payload: result,
        allow_simulated=True,
    )
    measured = list(provider(
        clients, candidates, bsses(), "2026-08-21T20:00:01.000Z"
    ))

    assert [item.sta_mac for item in measured] == [STA]
    assert all(item.rcpi is not None for item in measured)
    assert provider.last_rejected_candidate_keys == {(second_sta, BSSID)}


def test_rejection_must_match_agent_radio_and_requested_sta():
    result = response()
    result["metrics"] = []
    result["rejected"] = [{
        "agent_al": AGENT,
        "ruid": "02:00:00:00:ff:00",
        "sta": STA,
        "error_code": 1,
        "received_at_ms": 1787342400124,
        "message_id": 42,
    }]
    provider = ControllerCandidateProvider(
        "http://controller", requester=lambda _url, _payload: result,
        allow_simulated=True,
    )

    with pytest.raises(CandidateMetricsError, match="unexpected rejection"):
        list(provider(
            (client(),), (inventory(),), bsses(), "2026-08-21T20:00:01.000Z"
        ))
