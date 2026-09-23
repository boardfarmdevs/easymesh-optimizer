from copy import deepcopy

import pytest

from optimizer.counter_shadow import shadow_counter_trials
from optimizer.policy import PolicyConfig


def evidence():
    station, bssid = "02:00:00:00:03:00", "02:00:00:00:01:01"
    report = {"state": "passed", "medium_instance": "captured-instance", "station": station,
              "bssid": bssid, "frequency_mhz": 5180, "source_byte_unit": 1,
              "native_reports": [], "trials": []}
    for index, name in enumerate(("baseline", "data_loss", "ack_loss", "recovery")):
        rows = []
        for sequence in (0, 1):
            seconds = index * 10 + sequence
            rows.append({"received_at": 1800000000 + seconds, "monotonic_ns": (seconds + 1) * 10**9,
                         "source": "02:00:00:00:01:00", "transport": "ieee1905-ethernet",
                         "message_id": seconds, "loads": [{"bssid": bssid}],
                         "traffic": [{"sta_mac": station, "packets_sent": 400 * sequence,
                                      "packets_received": 0, "bytes_sent": 400000 * sequence, "bytes_received": 0,
                                      "retransmissions": 200 * sequence if "loss" in name else 0,
                                      "tx_packet_errors": 20 * sequence if "loss" in name else 0,
                                      "rx_packet_errors": 0}]})
        report["native_reports"].extend(rows)
        report["trials"].append({"name": name, "native_before": deepcopy(rows[0]),
                                 "native_after": deepcopy(rows[1]),
                                 "association_before": [bssid, 5180], "association_after": [bssid, 5180]})
    return report


def evaluate(report):
    return shadow_counter_trials(report, PolicyConfig(load_aware_enabled=True, load_counter_guard_enabled=True))


def test_real_report_shape_checks_clear_pressure_recovery_without_inventing_targets():
    result = evaluate(evidence())
    assert result["passed"] and not result["actuation"] and not result["load_target_selection_qualified"]
    assert [trial["decisions"][0]["state"] for trial in result["trials"]] == ["clear", "pressure", "pressure", "clear"]
    pressure = result["trials"][2]["decisions"][0]
    assert pressure["counter_deltas"]["retransmissions"] == 200
    assert pressure["evidence"]["counter_checks"][0]["value"] == 200
    assert pressure["evidence"]["epoch"] == "captured-instance"
    assert pressure["replay_at_report_receipt"]
    report = evidence()
    for trial in report["trials"]:
        trial.update(forward_snr=99, reverse_snr=99, expected_target="not-an-input")
    assert evaluate(report) == result


@pytest.mark.parametrize("invalid", ["owner", "epoch", "transport", "source", "bssid", "station", "window", "clock", "reset", "missing"])
def test_invalid_evidence_cannot_qualify_pressure(invalid):
    report = evidence()
    for trial in report["trials"]:
        if invalid == "owner":
            trial["association_after"][0] = "02:00:00:99:00:01"
    if invalid == "epoch":
        report["medium_instance"] = None
    for row in report["native_reports"][1::2]:
        if invalid == "transport":
            row["transport"] = "prpl-1905-broker"
        elif invalid == "source":
            row["source"] = "02:00:00:99:00:01"
        elif invalid == "bssid":
            row["loads"][0]["bssid"] = "02:00:00:99:00:01"
        elif invalid == "station":
            row["traffic"][0]["sta_mac"] = "02:00:00:99:00:01"
        elif invalid == "window":
            row["monotonic_ns"] += 20 * 10**9
        elif invalid == "clock":
            row["received_at"] -= 2
        elif invalid == "reset":
            row["traffic"][0]["retransmissions"] = (1 << 32) - 1
        elif invalid == "missing":
            del row["traffic"][0]["rx_packet_errors"]
    assert not evaluate(report)["passed"]


def test_no_native_pressure_is_not_a_qualification_and_disabled_policy_is_rejected():
    report = evidence()
    for row in report["native_reports"]:
        row["traffic"][0].update(retransmissions=0, tx_packet_errors=0)
    assert not evaluate(report)["passed"]
    with pytest.raises(ValueError, match="explicit counter guard"):
        shadow_counter_trials(report, PolicyConfig())
