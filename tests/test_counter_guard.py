from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from optimizer.config import load_policy
from optimizer.load_policy import policy_for
from optimizer.model import format_time, parse_time
from optimizer.policy import PolicyConfig, ThresholdPolicy
from .test_load_policy import loaded, policy


def counters(seconds=0, **changes):
    value = loaded(seconds)
    rates = dict(bytes_per_second=120000, retries_per_second=0,
                 tx_errors_per_second=0, rx_errors_per_second=0)
    rates.update(changes)
    return replace(value, client_activity=(replace(value.client_activity[0], **rates),))


def guarded(**changes):
    return policy(load_counter_guard_enabled=True, **changes)


def test_guard_is_explicit_and_signal_policy_does_not_consume_counters():
    assert type(policy_for(PolicyConfig())) is ThresholdPolicy
    sample = counters(retries_per_second=10000)
    assert ThresholdPolicy(PolicyConfig()).evaluate(sample).decisions[0].reason == "current_link_acceptable"
    assert policy(load_condition_hold_seconds=0).evaluate(sample).decisions[0].action == "steer"
    config = load_policy(Path(__file__).parents[1] / "configs/load-counter-guard-policy.yaml")
    assert config.load_counter_guard_enabled and config.load_aware_enabled
    for changes in ({"load_counter_guard_enabled": True},
                    {"load_aware_enabled": True, "load_counter_guard_enabled": 1},
                    {"load_maximum_retries_per_second": float("nan")},
                    {"load_maximum_rx_errors_per_second": -1}):
        with pytest.raises(ValueError):
            PolicyConfig(**changes)


@pytest.mark.parametrize("name,limit", [("retries_per_second", 100), ("tx_errors_per_second", 10),
                                       ("rx_errors_per_second", 10)])
def test_missing_zero_boundary_and_excess_are_distinct(name, limit):
    engine = guarded(load_condition_hold_seconds=0)
    for value, reason in ((None, "native_load_counter_evidence_unavailable"),
                          (0, "native_load_margin_hold_satisfied"),
                          (limit, "native_load_margin_hold_satisfied"),
                          (limit + 1, "native_load_counter_pressure")):
        decision = engine.evaluate(counters(**{name: value})).decisions[0]
        assert decision.reason == reason
        assert (decision.action == "steer") == (value is not None and value <= limit)
        check = next(row for row in decision.load_evidence["counter_checks"] if row["property"] == name)
        assert check["value"] == value
        assert decision.load_evidence["capacity_estimate"] is False


@pytest.mark.parametrize("changes", [
    {"epoch": "restarted"}, {"bssid": "02:00:00:99:00:01"}, {"source": "fixture"},
    {"observed_at": format_time(parse_time(loaded().observed_at) - timedelta(seconds=6))},
    {"observed_at": loaded(1).observed_at},
])
def test_owner_epoch_source_and_age_guards_remain_required(changes):
    sample = counters(**changes)
    decision = guarded(load_condition_hold_seconds=0).evaluate(sample).decisions[0]
    assert decision.reason == "native_load_activity_unavailable"
    assert decision.action == "none"


@pytest.mark.parametrize("changes", [{"observed_at": format_time(parse_time(loaded().observed_at) - timedelta(seconds=2))},
                                    {"transport": "prpl-1905-broker"}])
def test_counter_context_must_match_native_load(changes):
    decision = guarded(load_condition_hold_seconds=0).evaluate(counters(**changes)).decisions[0]
    assert decision.reason == "native_load_activity_context_mismatch"


def test_pressure_breaks_hold_and_cached_activity_cannot_satisfy_new_hold():
    engine = guarded(load_maximum_report_skew_seconds=5)
    first = engine.evaluate(counters())
    blocked = engine.evaluate(counters(4, retries_per_second=101), first.state)
    assert blocked.decisions[0].reason == "native_load_counter_pressure"
    fresh = engine.evaluate(counters(5), blocked.state)
    assert fresh.decisions[0].hold_seconds == 0
    cached = engine.evaluate(counters(10, observed_at=loaded(5).observed_at), fresh.state)
    assert cached.decisions[0].reason == "load_waiting_for_new_report"
    final = engine.evaluate(counters(10), cached.state)
    assert final.decisions[0].reason == "native_load_margin_hold_satisfied"


def test_counter_guard_keeps_signal_rescue_and_target_exclusions():
    engine = guarded(condition_hold_seconds=0, load_condition_hold_seconds=0)
    sample = counters(retries_per_second=1000)
    weak = replace(sample, clients=(replace(sample.clients[0], rcpi=80),))
    assert engine.evaluate(weak).decisions[0].reason == "threshold_margin_hold_satisfied"
    sample = counters()
    target = replace(sample.bss_loads[1], channel=sample.bss_loads[0].channel,
                     transport="prpl-1905-broker")
    decision = engine.evaluate(replace(sample, bss_loads=(sample.bss_loads[0], target))).decisions[0]
    assert decision.action == "none"
    reasons = decision.load_evidence["candidate_assessments"][0]["reasons"]
    assert "same_channel" in reasons and "transport_mismatch" in reasons


def test_received_scan_policies_disable_load_counter_guard():
    from optimizer.band_steering import band_policy

    config = guarded().config
    for same_band in (False, True):
        selected = band_policy(config, same_band=same_band)
        assert not selected.config.load_aware_enabled
        assert not selected.config.load_counter_guard_enabled
