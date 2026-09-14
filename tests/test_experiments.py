from __future__ import annotations

import json
from pathlib import Path

import pytest

from optimizer.cli import main
from optimizer.experiments import ExperimentError, build_matrix, verify_matrix
from optimizer.traffic import compile_traffic_plan


ROOT = Path(__file__).parents[1]
SPEC = ROOT / "scenarios" / "home-suite.json"


def test_home_matrix_is_deterministic_and_capability_gated():
    first = build_matrix(SPEC)
    second = build_matrix(SPEC)
    assert first == second
    verify_matrix(first)
    assert first["summary"] == {
        "cases": 148,
        "runnable": 56,
        "blocked": 92,
        "worlds": 10,
        "traffic_profiles": 5,
        "policies": 2,
        "seeds": 1,
    }
    band = [item for item in first["cases"] if item["scenario"] == "band-steer-5-to-6"]
    assert len(band) == 6
    assert all(item["status"] == "blocked" for item in band)
    assert all("frequency-qualified-snr" not in item["missing_capabilities"] for item in band)
    assert all(
        "cross-band-candidate-link-metrics" in item["missing_capabilities"]
        for item in band
    )
    assert all("band-candidate-inventory" not in item["missing_capabilities"] for item in band)
    assert all(item["truth_boundary"]["optimizer_inputs"] == "EasyMesh observations only" for item in first["cases"])
    twenty_client = [
        item for item in first["cases"]
        if item["world"]["counts"]["stations"] == 20
    ]
    assert twenty_client
    assert all(
        "scale-profile-small" in item["requirements"]
        and "scale-profile-small" not in item["missing_capabilities"]
        for item in twenty_client
    )


def test_world_layout_is_a_real_matrix_axis():
    matrix = build_matrix(SPEC)
    slow = [item for item in matrix["cases"] if item["scenario"] == "slow-walk-steering"]
    assert {item["world"]["layout"] for item in slow} == {
        "home-five-agent",
        "home-five-agent-shifted",
    }
    assert {item["traffic"]["id"] for item in slow} == {"latency-probe", "constant-load"}


def test_cli_writes_a_verified_matrix(tmp_path):
    output = tmp_path / "matrix.json"
    assert main(["matrix", "--spec", str(SPEC), "--output", str(output)]) == 0
    value = json.loads(output.read_text())
    verify_matrix(value)


def test_matrix_tamper_is_detected():
    matrix = build_matrix(SPEC)
    matrix["cases"][0]["seed"] += 1
    with pytest.raises(ExperimentError, match="matrix_sha256"):
        verify_matrix(matrix)


def test_small_latency_traffic_plan_is_bound_and_deterministic():
    matrix = build_matrix(SPEC)
    case = next(
        item for item in matrix["cases"]
        if item["scenario"] == "cartesian-home"
        and item["world"]["mobility"] == "stationary"
        and item["traffic"]["id"] == "latency-probe"
    )
    bindings = json.loads((ROOT / "scenarios" / "rev130-small-bindings.json").read_text())
    plan = compile_traffic_plan(matrix, case["id"], bindings)
    assert plan == compile_traffic_plan(matrix, case["id"], bindings)
    assert plan["status"] == "runnable"
    assert len(plan["events"]) == 10
    assert plan["events"][0]["command"][:4] == [
        "lxc", "exec", "wlan-client", "--"
    ]
    assert plan["events"][0]["duration_ms"] == 60_000


def test_idle_traffic_plan_has_no_processes():
    matrix = build_matrix(SPEC)
    case = next(
        item for item in matrix["cases"]
        if item["scenario"] == "cartesian-home"
        and item["world"]["mobility"] == "stationary"
        and item["traffic"]["id"] == "idle-keepalive"
    )
    bindings = json.loads((ROOT / "scenarios" / "rev130-small-bindings.json").read_text())
    assert compile_traffic_plan(matrix, case["id"], bindings)["events"] == []
