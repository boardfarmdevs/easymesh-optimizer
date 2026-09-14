from dataclasses import replace
from datetime import timedelta
import json
import math
import struct
from types import SimpleNamespace

import pytest

from optimizer.load_capture import NativeLoadDecoder
from optimizer.load_observer import NativeLoadProvider, inventory
from optimizer.load_policy import LoadAwarePolicy, policy_for
from optimizer.model import BssLoadObservation, ClientActivityObservation, Snapshot, format_time, parse_time
from optimizer.policy import PolicyConfig, ThresholdPolicy
from .helpers import snapshot, STA, SOURCE, TARGET


def loaded(seconds=0, **kwargs):
    value = snapshot(seconds, current_rcpi=150, target_rcpi=146, current_band="5", target_band="5", **kwargs)
    current, target = value.clients[0], value.candidates[0]
    return replace(value, schema_version=2, bss_loads=(
        BssLoadObservation(SOURCE, current.connected_device_id, "02:00:00:01:00:00", 36, 230, 2, value.observed_at, "epoch", 1),
        BssLoadObservation(TARGET, target.device_id, "02:00:00:02:00:00", 44, 70, 1, value.observed_at, "epoch", 1)),
        client_activity=(ClientActivityObservation(STA, SOURCE, 200, 1, value.observed_at, "epoch"),))


def policy(**changes):
    return LoadAwarePolicy(PolicyConfig(load_aware_enabled=True, expected_devices=5, expected_clients=10, **changes))


def decision(value, **changes):
    return policy(**changes).evaluate(value).decisions[0]


def test_default_is_exact_signal_policy_and_schema_one_stays_compatible():
    assert type(policy_for(PolicyConfig())) is ThresholdPolicy
    value = snapshot(0)
    assert "bss_loads" not in value.to_dict()
    assert Snapshot.from_dict(value.to_dict()) == value
    with pytest.raises(ValueError, match="schema 2"):
        replace(loaded(), schema_version=1)
    assert Snapshot.from_dict(loaded().to_dict()) == loaded()


def test_sustained_native_load_can_choose_slightly_weaker_quieter_channel():
    engine = policy()
    first = engine.evaluate(loaded())
    assert first.decisions[0].reason == "load_condition_hold_not_met"
    second = engine.evaluate(loaded(5), first.state)
    chosen = second.decisions[0]
    assert chosen.action == "steer"
    assert chosen.reason == "native_load_margin_hold_satisfied"
    assert chosen.target_rcpi < chosen.current_rcpi
    assert chosen.load_evidence["target_utilization"] == 70
    assert chosen.load_evidence["capacity_estimate"] is False
    assert engine.evaluate(loaded(6), second.state).decisions[0].reason == "steer_pending"


def test_hold_requires_new_native_reports_and_resets_on_provider_epoch():
    engine = policy()
    first = engine.evaluate(loaded())
    cached = replace(loaded(), observed_at=loaded(5).observed_at)
    assert engine.evaluate(cached, first.state).decisions[0].reason == "load_waiting_for_new_report"
    changed = loaded(5)
    changed = replace(changed, bss_loads=tuple(replace(row, epoch="replacement") for row in changed.bss_loads),
                      client_activity=tuple(replace(row, epoch="replacement") for row in changed.client_activity))
    assert engine.evaluate(changed, first.state).decisions[0].reason == "load_condition_hold_not_met"


def test_cli_load_receiver_is_opt_in_owned_and_closed_on_failure(monkeypatch):
    from optimizer import cli
    calls = []
    class Receiver:
        def __init__(self, controller):
            calls.append(controller)

        def close(self):
            calls.append("closed")

    args = SimpleNamespace(policy="unused", backend="prplmesh", candidate_provider="controller")
    monkeypatch.setattr(cli, "NativeLoadProvider", Receiver)
    monkeypatch.setattr(cli, "load_policy", lambda _path: PolicyConfig(load_aware_enabled=True))
    monkeypatch.setattr(cli, "_live_run", lambda *_args: (_value for _value in ()).throw(OSError("test")))
    with pytest.raises(OSError, match="test"):
        cli._live(args, "recommend")
    assert len(calls) == 2 and calls[-1] == "closed"
    calls.clear()
    args.candidate_provider = "off"
    with pytest.raises(SystemExit, match="candidate-provider"):
        cli._live(args, "recommend")
    assert calls == []
    monkeypatch.setattr(cli, "load_policy", lambda _path: PolicyConfig())
    monkeypatch.setattr(cli, "_live_run", lambda *_args: 0)
    assert cli._live(args, "recommend") == 0
    assert calls == []


@pytest.mark.parametrize("field,value", [
    ("source", "fixture"), ("epoch", "other"), ("channel", 36), ("utilization", 190),
    ("backhaul_hops", 2), ("backhaul_hops", None),
])
def test_target_guards(field, value):
    value_snapshot = loaded()
    target = replace(value_snapshot.bss_loads[1], **{field: value})
    result = decision(replace(value_snapshot, bss_loads=(value_snapshot.bss_loads[0], target)), load_condition_hold_seconds=0)
    assert result.action == "none"
    assert result.reason == "native_load_no_safe_quieter_target"


def test_missing_stale_and_skewed_load_never_becomes_zero():
    value = loaded()
    assert decision(replace(value, bss_loads=())).reason == "native_load_current_unavailable"
    old = format_time(parse_time(value.observed_at) - timedelta(seconds=6))
    assert decision(replace(value, bss_loads=(replace(value.bss_loads[0], observed_at=old), value.bss_loads[1]))).reason == "native_load_current_unavailable"
    skewed = replace(value.bss_loads[1], observed_at=format_time(parse_time(value.observed_at) - timedelta(seconds=2)))
    assert decision(replace(value, bss_loads=(value.bss_loads[0], skewed))).reason == "native_load_no_safe_quieter_target"


def test_idle_unknown_or_reassociated_activity_cannot_drive_load_steering():
    value = loaded()
    for rows in ((), (replace(value.client_activity[0], packets_per_second=0),),
                 (replace(value.client_activity[0], bssid=TARGET),),
                 (replace(value.client_activity[0], source="fixture"),)):
        assert decision(replace(value, client_activity=rows), load_condition_hold_seconds=0).action == "none"


def test_five_second_cadence_profile_keeps_freshness_and_longer_hold():
    engine = policy(load_maximum_report_skew_seconds=5, load_condition_hold_seconds=10)

    def staggered(seconds):
        value = loaded(seconds)
        target = replace(value.bss_loads[1], observed_at=format_time(
            parse_time(value.observed_at) - timedelta(seconds=2)))
        return replace(value, bss_loads=(value.bss_loads[0], target))

    first = engine.evaluate(staggered(0))
    second = engine.evaluate(staggered(5), first.state)
    assert second.decisions[0].action == "none"
    final = engine.evaluate(staggered(10), second.state)
    assert final.decisions[0].reason == "native_load_margin_hold_satisfied"
    assert final.decisions[0].load_evidence["candidate_report_skew_seconds"][TARGET] == 2
    stale = replace(staggered(10), observed_at=loaded(16).observed_at)
    assert engine.evaluate(stale, second.state).decisions[0].action == "none"


def test_quiet_current_ap_does_not_ping_pong_to_stronger_busy_ap():
    value = loaded()
    quiet = replace(value.bss_loads[0], utilization=50)
    assert decision(replace(value, bss_loads=(quiet, value.bss_loads[1]))).reason == "native_load_current_acceptable"


def test_rf_emergency_still_uses_signal_policy():
    value = loaded()
    value = replace(value, clients=(replace(value.clients[0], rcpi=80),), bss_loads=(), client_activity=())
    assert decision(value, condition_hold_seconds=0).reason == "threshold_margin_hold_satisfied"


def test_only_one_active_client_balances_and_settling_is_global():
    value = loaded()
    second_mac = "02:00:00:00:04:00"
    value = replace(value, health=replace(value.health, clients=2),
        clients=value.clients + (replace(value.clients[0], sta_mac=second_mac),),
        candidates=value.candidates + (replace(value.candidates[0], sta_mac=second_mac),),
        client_activity=value.client_activity + (replace(value.client_activity[0], sta_mac=second_mac, packets_per_second=400),))
    engine = policy(require_complete_client_roster=False, load_condition_hold_seconds=0)
    result = engine.evaluate(value)
    actions = [row for row in result.decisions if row.action == "steer"]
    assert len(actions) == 1 and actions[0].sta_mac == second_mac
    assert engine.evaluate(value, result.state).decisions[0].reason == "native_load_settling"


@pytest.mark.parametrize("number", [math.nan, math.inf, -1])
def test_invalid_load_config_and_activity_rejected(number):
    with pytest.raises(ValueError):
        PolicyConfig(load_condition_hold_seconds=number)
    with pytest.raises(ValueError):
        replace(loaded().client_activity[0], packets_per_second=number)


def frame(payload, fragment=0, last=True, identifier=1):
    header = bytes.fromhex("0180c2000013 020000000920 893a")
    return header + struct.pack("!BBHHBB", 0, 0, 0x800C, identifier, fragment, 0x80 if last else 0) + payload


def tlv(kind, data):
    return struct.pack("!BH", kind, len(data)) + data


def test_native_decoder_fragments_units_duplicates_and_malformed_messages():
    load = tlv(0x94, bytes.fromhex(SOURCE.replace(":", "")) + struct.pack("!BHB", 255, 3, 0))
    traffic = tlv(0xA2, bytes.fromhex(STA.replace(":", "")) + struct.pack("!IIIIIII", 123, 456, 7, 8, 0, 0, 0))
    payload = load + traffic + bytes(3)
    decoder = NativeLoadDecoder()
    assert decoder.feed(frame(payload[:9], last=False), 1) is None
    report = decoder.feed(frame(payload[9:], fragment=1), 1.1)
    assert report["loads"][0]["utilization"] == 255
    assert report["traffic"][0]["packets_sent"] == 7
    assert decoder.feed(frame(payload[9:], fragment=1), 1.2) is None
    assert decoder.feed(frame(payload[:-2], identifier=2), 2) is None
    assert decoder.feed(frame(payload[:9], last=False, identifier=3), 3) is None
    assert decoder.feed(frame(payload[9:], fragment=1, identifier=3), 9) is None


def test_native_provider_epoch_reset_source_and_context_guards(tmp_path):
    status = tmp_path / "bridge.json"
    status.write_text(json.dumps({"schema": "easymesh.rf-survey-bridge.v1", "source": "wmediumd-modeled-airtime",
                                  "instance_id": "one", "recorded_monotonic_ns": 1000000000}))
    provider = NativeLoadProvider(provenance_path=status)
    value = loaded()
    current = value.clients[0]
    raw = {"topology": {"nodes": [{"id": current.connected_device_id}], "edges": []},
           "bsses": {"bsses": [{"bssid": SOURCE, "device_id": current.connected_device_id,
                                "radio_id": "02:00:00:01:00:00", "channel": 36, "ssid": "private_ssid"}]}}
    assert provider.enrich(value, raw, now_ns=1000000000).bss_loads == ()
    base = {"source": current.connected_device_id, "received_at": parse_time(value.observed_at).timestamp(),
            "loads": [{"bssid": SOURCE, "utilization": 200, "station_count": 1}],
            "traffic": [{"sta_mac": STA, "packets_sent": 1, "packets_received": 2}]}
    provider.ingest({**base, "monotonic_ns": 1100000000})
    provider.ingest({**base, "received_at": base["received_at"] + .5, "monotonic_ns": 1600000000, "traffic": [
        {"sta_mac": STA, "packets_sent": 51, "packets_received": 52}]})
    result = provider.enrich(value, raw, now_ns=1700000000)
    assert result.bss_loads[0].utilization == 200
    assert result.client_activity[0].packets_per_second == 200
    raw["bsses"]["bsses"][0]["channel"] = 44
    assert provider.enrich(value, raw, now_ns=1800000000).bss_loads == ()
    assert provider.enrich(value, raw, now_ns=3000000000).bss_loads == ()
    status.write_text(json.dumps({"schema": "easymesh.rf-survey-bridge.v1", "source": "fixed-fixture",
                                  "instance_id": "one", "recorded_monotonic_ns": 1800000000}))
    assert provider.enrich(value, raw, now_ns=1800000000).bss_loads == ()
