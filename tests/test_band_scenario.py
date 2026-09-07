from __future__ import annotations

import json
from pathlib import Path

import pytest

from optimizer.policy import PolicyConfig, ThresholdPolicy
from optimizer.state import PolicyState
from .helpers import snapshot


@pytest.mark.scenario
@pytest.mark.parametrize(("source_band", "target_band"), [("2.4", "5"), ("5", "6")])
def test_small_band_walk_yields_one_bssid_recommendation(source_band, target_band):
    root = Path(__file__).resolve().parents[2]
    world_path = (
        root / "wmediumd" / "configurator" / "worlds" / "golden"
        / "home-a-band-walk-small.world.json"
    )
    world = json.loads(world_path.read_text(encoding="utf-8"))
    assert world["schema"] == "wmdcfg.world-plan.v1"
    assert "band-walk" in world["tags"]
    assert world["counts"] == {"agents": 5, "stations": 10}

    # Measurement-layer fixtures are deliberately independent of the world's
    # SNR values. The policy may consume only the EasyMesh-shaped observations.
    observations = [
        snapshot(0, current_rcpi=130, target_rcpi=126,
                 current_band=source_band, target_band=target_band),
        snapshot(5, current_rcpi=130, target_rcpi=126,
                 current_band=source_band, target_band=target_band),
    ]
    engine = ThresholdPolicy(PolicyConfig(band_upgrade_enabled=True))
    state = PolicyState()
    decisions = []
    for observed in observations:
        result = engine.evaluate(observed, state)
        state = result.state
        decisions.extend(result.decisions)

    actions = [item for item in decisions if item.action == "steer"]
    assert len(actions) == 1
    assert actions[0].reason == "band_preference_hold_satisfied"
    assert actions[0].target_band == target_band
    assert actions[0].target_bssid is not None
