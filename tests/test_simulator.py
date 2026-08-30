from __future__ import annotations

import json
from pathlib import Path

from optimizer.policy import PolicyConfig, ThresholdPolicy
from optimizer.simulator import SimulationConfig, WorldSimulator


ROOT = Path(__file__).parents[1]
WORLD = (
    ROOT.parent / "wmediumd" / "configurator" / "worlds" / "golden"
    / "home-a-band-walk-small.world.json"
)


def world():
    return json.loads(WORLD.read_text())


def policy(**changes):
    values = {
        "expected_devices": 5,
        "expected_clients": 10,
        "minimum_dwell_seconds": 0,
        "condition_hold_seconds": 0,
        "reject_stale_metrics_after_seconds": 3,
        "band_upgrade_enabled": True,
        "minimum_band_upgrade_target_rcpi": 100,
        "maximum_band_upgrade_loss_rcpi": 16,
    }
    values.update(changes)
    return ThresholdPolicy(PolicyConfig(**values))


def test_simulation_is_deterministic_and_keeps_truth_explicitly_synthetic():
    first = WorldSimulator(world(), policy()).run()
    second = WorldSimulator(world(), policy()).run()
    assert first == second
    assert first["truth_boundary"] == {
        "kind": "synthetic_test_telemetry",
        "live_observer_compatible": False,
        "production_optimizer_reads_world": False,
    }
    assert first["summary"]["cycles"] == 30
    assert len(first["summary"]["final_associations"]) == 10


def test_band_policy_can_move_clients_from_24_to_measured_higher_band_bssids():
    result = WorldSimulator(
        world(), policy(), config=SimulationConfig(initial_band="2.4")
    ).run()
    assert result["summary"]["attempts"] > 0
    assert result["summary"]["accepted"] == result["summary"]["attempts"]
    assert any(
        association["band"] in {"5", "6"}
        for association in result["summary"]["final_associations"].values()
    )
    assert any(
        decision["reason"] == "band_preference_hold_satisfied"
        for cycle in result["cycles"]
        for decision in cycle["evaluation"]["decisions"]
    )


def test_ignored_client_produces_timeout_and_backoff_without_repeat_storm():
    result = WorldSimulator(
        world(),
        policy(
            steer_timeout_seconds=2,
            failure_backoff_seconds=20,
            maximum_failure_backoff_seconds=20,
        ),
        config=SimulationConfig(initial_band="2.4"),
        client_behavior={"sta_static_01": "ignore"},
    ).run()
    ignored = [
        item
        for cycle in result["cycles"]
        for item in cycle["outcomes"]
        if item["sta_role"] == "sta_static_01"
    ]
    assert ignored
    reasons = [
        item["reason"]
        for cycle in result["cycles"]
        for item in cycle["evaluation"]["decisions"]
        if item["sta_mac"] == ignored[0]["sta_mac"]
    ]
    assert "steer_timeout_backoff" in reasons
    assert "steer_failure_backoff" in reasons
    assert len(ignored) <= 3
