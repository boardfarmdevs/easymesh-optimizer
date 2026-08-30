from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from optimizer.model import CandidateObservation, Snapshot
from optimizer.policy import PolicyConfig, ThresholdPolicy
from optimizer.state import PolicyState
from wmdcfg.parser import parse
from .helpers import snapshot


@pytest.mark.scenario
def test_existing_two_ap_crossover_yields_one_report_based_recommendation():
    root = Path(__file__).resolve().parents[2]
    scenario_path = root / "wmediumd" / "configurator" / "scenarios" / "two-ap-crossover.wmd"
    scenario = parse(scenario_path.read_text(encoding="utf-8"))
    assert [phase.name for phase in scenario.phases] == [
        "baseline",
        "crossover",
        "destination_hold",
    ]
    assert sum(phase.duration_ms for phase in scenario.phases) == 60_000

    fixture = Path(__file__).parent / "fixtures" / "two-ap-crossover-observations.jsonl"
    snapshots = [
        Snapshot.from_dict(json.loads(line))
        for line in fixture.read_text(encoding="utf-8").splitlines()
        if line
    ]
    # These are recorded controller/EasyMesh observations. The optimizer test
    # intentionally never derives RCPI or a target from the scenario's SNR.
    assert {
        item.measurement_source
        for snapshot in snapshots
        for item in (*snapshot.clients, *snapshot.candidates)
    } == {"associated_sta_link_metrics", "beacon_metrics_response"}

    engine = ThresholdPolicy(PolicyConfig())
    state = PolicyState()
    decisions = []
    for snapshot in snapshots:
        evaluated = engine.evaluate(snapshot, state)
        state = evaluated.state
        decisions.extend(evaluated.decisions)
    actionable = [item for item in decisions if item.action == "steer"]
    assert len(actionable) == 1
    assert actionable[0].reason == "threshold_margin_hold_satisfied"


@pytest.mark.scenario
def test_live_acceptance_crossover_isolates_one_target_in_five_agent_lab():
    root = Path(__file__).resolve().parents[2]
    scenario_path = (
        root
        / "wmediumd"
        / "configurator"
        / "scenarios"
        / "optimizer-five-ap-crossover.wmd"
    )
    scenario = parse(scenario_path.read_text(encoding="utf-8"))

    assert scenario.roles == {
        "client": "station",
        "source": "fronthaul_ap",
        "target": "fronthaul_ap",
        "alternate_1": "fronthaul_ap",
        "alternate_2": "fronthaul_ap",
        "alternate_3": "fronthaul_ap",
    }
    assert [phase.name for phase in scenario.phases] == [
        "baseline",
        "crossover",
        "destination_hold",
    ]
    assert sum(phase.duration_ms for phase in scenario.phases) == 130_000

    baseline_links = [
        action for action in scenario.phases[0].actions
        if hasattr(action, "source") and hasattr(action, "destination")
    ]
    assert len(baseline_links) == 5
    assert {action.start_snr_db for action in baseline_links} == {10, 42}


@pytest.mark.scenario
def test_five_ap_measured_stream_recommends_only_the_unique_target():
    """Exercise policy with four targets without importing scenario SNR."""
    target_bssids = (
        "02:00:00:bb:bb:01",
        "02:00:00:cc:cc:01",
        "02:00:00:dd:dd:01",
        "02:00:00:ee:ee:01",
    )

    def measured(seconds, current, values):
        base = snapshot(seconds, current_rcpi=current, target_rcpi=values[0])
        candidates = tuple(
            CandidateObservation(
                sta_mac=base.clients[0].sta_mac,
                bssid=bssid,
                device_id=f"02:00:00:00:{8 - index:02x}:20",
                device_name=f"candidate-{index + 1}",
                rcpi=rcpi,
                metric_observed_at=base.observed_at,
                measurement_source=(
                    "easy_mesh_unassociated_sta_link_metrics:"
                    "hwsim-wmediumd-read-only:simulated"
                ),
                band="5",
            )
            for index, (bssid, rcpi) in enumerate(zip(target_bssids, values))
        )
        return replace(base, candidates=candidates)

    observations = (
        measured(0, 138, (74, 72, 70, 68)),
        measured(30, 84, (136, 82, 78, 74)),
        measured(36, 84, (136, 82, 78, 74)),
    )
    policy = ThresholdPolicy(PolicyConfig())
    state = PolicyState()
    decisions = []
    for observed in observations:
        evaluation = policy.evaluate(observed, state)
        state = evaluation.state
        decisions.extend(evaluation.decisions)

    actions = [item for item in decisions if item.action == "steer"]
    assert len(actions) == 1
    assert actions[0].target_bssid == target_bssids[0]
    assert actions[0].scores[0].bssid == target_bssids[0]
    assert {item.measurement_source for item in observations[-1].candidates} == {
        "easy_mesh_unassociated_sta_link_metrics:"
        "hwsim-wmediumd-read-only:simulated"
    }
