from __future__ import annotations

from optimizer.policy import PolicyConfig, ThresholdPolicy
from optimizer.state import PolicyState
from .helpers import TARGET, snapshot


def policy(**changes):
    values = {
        "expected_clients": 10,
        "condition_hold_seconds": 5,
        "minimum_dwell_seconds": 20,
        "reject_stale_metrics_after_seconds": 7,
    }
    values.update(changes)
    return ThresholdPolicy(PolicyConfig(**values))


def decision(result):
    return result.decisions[0]


def test_missing_metric_freshness_is_a_safe_no_action():
    result = policy().evaluate(snapshot(0, metric_age=None))
    assert decision(result).action == "none"
    assert decision(result).reason == "current_metric_freshness_unknown"


def test_stale_candidate_is_a_safe_no_action():
    result = policy().evaluate(snapshot(0, target_age=8))
    assert decision(result).reason == "fresh_candidate_metric_missing"


def test_threshold_margin_and_hold_produce_exactly_one_pending_recommendation():
    first = policy().evaluate(snapshot(0))
    assert decision(first).reason == "condition_hold_not_met"
    second = policy().evaluate(snapshot(5), first.state)
    assert decision(second).action == "steer"
    assert decision(second).target_bssid == TARGET
    third = policy().evaluate(snapshot(6), second.state)
    assert decision(third).action == "none"
    assert decision(third).reason == "steer_pending"


def test_dwell_and_health_are_gates():
    assert decision(policy().evaluate(snapshot(0, association_uptime=19))).reason == "minimum_dwell_not_met"
    assert decision(policy().evaluate(snapshot(0, devices=4))).reason == "mesh_device_count_mismatch"


def test_observed_target_association_enters_cooldown():
    first = policy().evaluate(snapshot(0))
    pending = policy().evaluate(snapshot(5), first.state)
    moved = policy().evaluate(snapshot(6, source=TARGET), pending.state)
    assert decision(moved).reason == "target_association_observed"
    assert moved.state.for_sta(snapshot(0).clients[0].sta_mac).phase == "cooldown"


def test_band_upgrade_is_a_bssid_decision_with_safety_floor_and_hold():
    engine = policy(
        band_upgrade_enabled=True,
        minimum_band_upgrade_target_rcpi=120,
        maximum_band_upgrade_loss_rcpi=8,
    )
    first = engine.evaluate(
        snapshot(0, current_rcpi=130, target_rcpi=126,
                 current_band="2.4", target_band="5")
    )
    assert decision(first).reason == "condition_hold_not_met"
    assert decision(first).target_band == "5"
    assert decision(first).scores[0].band_rank_delta == 1
    second = engine.evaluate(
        snapshot(5, current_rcpi=130, target_rcpi=126,
                 current_band="2.4", target_band="5"),
        first.state,
    )
    assert decision(second).action == "steer"
    assert decision(second).reason == "band_preference_hold_satisfied"
    assert decision(second).target_bssid == TARGET
    assert decision(second).target_band == "5"


def test_band_upgrade_abstains_when_candidate_is_too_weak():
    result = policy(band_upgrade_enabled=True).evaluate(
        snapshot(0, current_rcpi=130, target_rcpi=118,
                 current_band="5", target_band="6")
    )
    assert decision(result).action == "none"
    assert decision(result).reason == "no_safe_band_upgrade"


def test_band_upgrade_can_select_a_measured_6ghz_bssid():
    engine = policy(band_upgrade_enabled=True, condition_hold_seconds=0)
    result = engine.evaluate(
        snapshot(0, current_rcpi=132, target_rcpi=126,
                 current_band="5", target_band="6")
    )
    assert decision(result).action == "steer"
    assert decision(result).target_band == "6"
    assert decision(result).scores[0].band_rank_delta == 1


def test_default_policy_does_not_upgrade_an_acceptable_link():
    result = policy().evaluate(
        snapshot(0, current_rcpi=130, target_rcpi=130,
                 current_band="2.4", target_band="5")
    )
    assert decision(result).reason == "current_link_acceptable"


def test_ignored_btm_times_out_into_exponential_failure_backoff():
    engine = policy(
        condition_hold_seconds=0,
        steer_timeout_seconds=10,
        failure_backoff_seconds=20,
        maximum_failure_backoff_seconds=80,
    )
    pending = engine.evaluate(snapshot(0))
    assert decision(pending).action == "steer"

    timed_out = engine.evaluate(snapshot(11), pending.state)
    assert decision(timed_out).reason == "steer_timeout_backoff"
    failed = timed_out.state.for_sta(snapshot(0).clients[0].sta_mac)
    assert failed.phase == "backoff"
    assert failed.failure_count == 1
    assert failed.last_failure_reason == "association_timeout"

    inhibited = engine.evaluate(snapshot(30), timed_out.state)
    assert decision(inhibited).reason == "steer_failure_backoff"

    retry = engine.evaluate(snapshot(31), timed_out.state)
    assert decision(retry).action == "steer"
    second_timeout = engine.evaluate(snapshot(42), retry.state)
    second = second_timeout.state.for_sta(snapshot(0).clients[0].sta_mac)
    assert second.failure_count == 2
    assert decision(second_timeout).reason == "steer_timeout_backoff"
    assert parse_backoff_seconds(second.backoff_until, 42) == 40


def parse_backoff_seconds(value, seconds):
    from optimizer.model import parse_time

    return int((parse_time(value) - parse_time(snapshot(seconds).observed_at)).total_seconds())


def test_parse_time_accepts_rfc3339_nanoseconds():
    from optimizer.model import parse_time

    parsed = parse_time("2026-08-22T02:31:31.496925791+00:00")

    assert parsed.isoformat() == "2026-08-22T02:31:31.496925+00:00"
