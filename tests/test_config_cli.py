from __future__ import annotations

from pathlib import Path
import json

import pytest

import optimizer.cli as cli
from optimizer.cli import _live_policy, _recommendation_state, main, parser
from optimizer.config import load_policy
from optimizer.policy import PolicyConfig, ThresholdPolicy
from optimizer.recorder import Journal
from .helpers import snapshot


def test_example_policy_loads_without_external_yaml_dependency():
    config = load_policy(Path(__file__).parents[1] / "configs" / "threshold-policy.yaml")
    assert config.policy_version == 1
    assert config.current_rcpi_below == 100
    assert config.expected_clients == 20
    assert config.band_upgrade_enabled is False

    band = load_policy(Path(__file__).parents[1] / "configs" / "band-upgrade-policy.yaml")
    assert band.band_upgrade_enabled is True
    assert band.minimum_band_upgrade_target_rcpi == 120


def test_replay_is_byte_deterministic(tmp_path):
    source = tmp_path / "capture.jsonl"
    journal = Journal(source)
    for item in (
        snapshot(0, clients=20), snapshot(5, clients=20), snapshot(6, clients=20)
    ):
        journal.append("snapshot", item.to_dict(), recorded_at=item.observed_at)

    policy = Path(__file__).parents[1] / "configs" / "threshold-policy.yaml"
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    assert main(
        ["replay", "--input", str(source), "--policy", str(policy), "--expected-devices", "5",
         "--journal", str(first)]
    ) == 0
    assert main(
        ["replay", "--input", str(source), "--policy", str(policy), "--expected-devices", "5",
         "--journal", str(second)]
    ) == 0
    assert first.read_bytes() == second.read_bytes()


def test_evaluate_accepts_plain_snapshot_and_persists_state(tmp_path):
    source = tmp_path / "snapshot.json"
    output = tmp_path / "evaluation.json"
    state = tmp_path / "state.json"
    source.write_text(
        json.dumps(snapshot(0, clients=20).to_dict()), encoding="utf-8"
    )
    policy = Path(__file__).parents[1] / "configs" / "threshold-policy.yaml"

    assert main([
        "evaluate", "--input", str(source), "--policy", str(policy), "--expected-devices", "5",
        "--output", str(output), "--state-out", str(state),
    ]) == 0

    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["schema"] == "optimizer.evaluation.v1"
    assert result["evaluation"]["decisions"][0]["reason"] == "condition_hold_not_met"
    assert json.loads(state.read_text(encoding="utf-8"))["clients"][0]["phase"] == "holding"


def test_evaluate_rejects_an_unversioned_ad_hoc_input(tmp_path):
    source = tmp_path / "bad.json"
    source.write_text('{"clients": []}', encoding="utf-8")
    policy = Path(__file__).parents[1] / "configs" / "threshold-policy.yaml"
    with pytest.raises(SystemExit, match="invalid snapshot input"):
        main([
            "evaluate", "--input", str(source), "--policy", str(policy), "--expected-devices", "5",
            "--output", str(tmp_path / "never.json"),
        ])


def test_recommendation_state_does_not_invent_an_executed_action():
    policy = ThresholdPolicy(PolicyConfig(expected_devices=5, 
        expected_clients=10,
        condition_hold_seconds=5,
        minimum_dwell_seconds=20,
    ))
    first = policy.evaluate(snapshot(0))
    proposed = policy.evaluate(snapshot(5), first.state)
    assert proposed.decisions[0].action == "steer"
    retained = _recommendation_state(first.state, proposed)
    assert retained.for_sta(snapshot(0).clients[0].sta_mac).phase == "recommended"

    repeated = policy.evaluate(snapshot(6), retained)
    assert repeated.decisions[0].action == "none"
    assert repeated.decisions[0].reason == "recommendation_unchanged"


def test_act_mode_is_bounded_to_one_attempt_by_default():
    args = parser().parse_args([
        "act", "--journal", "/tmp/journal", "--policy", "/tmp/policy",
    ])
    assert args.max_actions == 1
    with pytest.raises(SystemExit):
        parser().parse_args([
            "act", "--journal", "/tmp/journal", "--policy", "/tmp/policy",
            "--max-actions", "0",
        ])


def test_a_policy_names_no_mesh_size_and_the_cli_refuses_without_the_labs():
    policy_path = Path(__file__).parents[1] / "configs" / "threshold-policy.yaml"
    for name in ("threshold-policy", "load-aware-policy", "load-aware-policy-prplmesh",
                 "load-counter-guard-policy", "band-upgrade-policy"):
        assert load_policy(policy_path.with_name(name + ".yaml")).expected_devices is None
    with pytest.raises(SystemExit, match="--expected-devices"):
        _live_policy(str(policy_path), None)
    # 5, 6 (a wired extender) and 8 (and two OpenSync pods): the lab's count, as given
    for devices in (5, 6, 8):
        assert _live_policy(str(policy_path), None, devices).config.expected_devices == devices


def test_live_policy_preserves_default_and_accepts_profile_override():
    policy_path = Path(__file__).parents[1] / "configs" / "threshold-policy.yaml"
    assert _live_policy(str(policy_path), None, 5).config.expected_clients == 20
    assert _live_policy(str(policy_path), 50, 5).config.expected_clients == 50
    assert _live_policy(str(policy_path), None, 6).config.expected_devices == 6
    assert parser().parse_args(["recommend", "--journal", "/tmp/journal", "--policy", str(policy_path),
                                "--expected-devices", "6"]).expected_devices == 6

    args = parser().parse_args([
        "recommend", "--journal", "/tmp/journal", "--policy", str(policy_path),
        "--expected-clients", "50",
    ])
    assert args.expected_clients == 50
    with pytest.raises(SystemExit):
        parser().parse_args([
            "recommend", "--journal", "/tmp/journal", "--policy", str(policy_path),
            "--expected-clients", "0",
        ])


def test_live_continue_records_passive_api_failure_without_evaluating(
    tmp_path, monkeypatch
):
    class FailedObserver:
        last_raw = None

        def __init__(self, *args, **kwargs):
            pass

        def observe(self):
            raise OSError("topology API unavailable")

    monkeypatch.setattr(cli, "ControllerObserver", FailedObserver)
    journal = tmp_path / "failed-observation.jsonl"
    assert main([
        "observe", "--journal", str(journal), "--count", "2", "--interval", "0",
        "--backend", "rdk", "--observation-error-policy", "continue",
    ]) == 0

    records = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [item["kind"] for item in records] == [
        "observation_error", "observation_error",
    ]
    assert all(item["payload"]["error_type"] == "OSError" for item in records)
