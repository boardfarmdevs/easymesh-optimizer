from __future__ import annotations

from optimizer.planners import (
    BackhaulEdgeObservation,
    RadioEnvironmentObservation,
    plan_backhaul,
    recommend_channel_width,
)
from optimizer.cli import main
import json


NOW = "2026-08-20T20:00:00Z"
GW = "02:00:00:00:00:20"
A1 = "02:00:00:00:01:20"
A2 = "02:00:00:00:02:20"


def edge(left, right, band, snr, rate=600, utilization=20, retries=2, at=NOW):
    return BackhaulEdgeObservation(
        left=left,
        right=right,
        band=band,
        snr_db=snr,
        phy_rate_mbps=rate,
        utilization_percent=utilization,
        retry_percent=retries,
        observed_at=at,
    )


def test_backhaul_planner_selects_one_band_per_edge_and_a_loop_free_tree():
    result = plan_backhaul(
        root=GW,
        nodes=[GW, A1, A2],
        observed_at=NOW,
        edges=[
            edge(GW, A1, "2.4", 35, rate=100),
            edge(GW, A1, "5", 34, rate=700),
            edge(GW, A1, "6", 31, rate=1200),
            edge(GW, A2, "5", 28, rate=600),
            edge(A1, A2, "5", 40, rate=800),
            edge(A1, A2, "6", 38, rate=1200),
        ],
    )
    assert result.status == "recommend"
    assert len(result.edges) == 2
    assert {(item.left, item.right) for item in result.edges} == {
        tuple(sorted((GW, A1))),
        tuple(sorted((A1, A2))),
    }
    assert all(item.band == "6" for item in result.edges)


def test_backhaul_24ghz_is_available_as_an_explicit_connectivity_failsafe():
    result = plan_backhaul(
        root=GW,
        nodes=[GW, A1, A2],
        observed_at=NOW,
        edges=[edge(GW, A1, "5", 30), edge(A1, A2, "2.4", 20, rate=80)],
    )
    assert result.status == "recommend"
    assert {item.band for item in result.edges} == {"2.4", "5"}


def test_backhaul_planner_abstains_if_fresh_viable_graph_is_disconnected():
    result = plan_backhaul(
        root=GW,
        nodes=[GW, A1, A2],
        observed_at=NOW,
        edges=[edge(GW, A1, "5", 4)],
    )
    assert result.status == "blocked"
    assert result.reason == "fresh_viable_graph_disconnected"
    assert result.edges == ()
    assert result.rejected_weak == 1


def environment(band, current, allowed, primary, secondary, neighbors, radar=0):
    return RadioEnvironmentObservation(
        radio_id="02:00:00:aa:bb:cc",
        band=band,
        current_width_mhz=current,
        allowed_widths_mhz=allowed,
        primary_utilization_percent=primary,
        secondary_utilization_percent=secondary,
        overlapping_neighbor_percent=neighbors,
        radar_risk=radar,
        observed_at=NOW,
    )


def test_width_planner_allows_24ghz_40_only_in_a_clean_location():
    clean = recommend_channel_width(
        environment("2.4", 20, (20, 40), 10, 5, 5), now=NOW
    )
    busy = recommend_channel_width(
        environment("2.4", 40, (20, 40), 40, 30, 60), now=NOW
    )
    assert (clean.action, clean.target_width_mhz) == ("recommend", 40)
    assert (busy.action, busy.target_width_mhz) == ("recommend", 20)


def test_width_planner_caps_5ghz_near_radar_and_prefers_clean_6ghz_160():
    radar = recommend_channel_width(
        environment("5", 80, (20, 40, 80, 160), 20, 10, 10, radar=0.8), now=NOW
    )
    clean6 = recommend_channel_width(
        environment("6", 80, (20, 40, 80, 160), 20, 10, 10), now=NOW
    )
    assert radar.target_width_mhz == 40
    assert radar.reason == "radar_risk_width_cap"
    assert clean6.target_width_mhz == 160
    assert clean6.reason == "clean_6ghz_wide_channel"


def test_planner_cli_writes_recommendation_only_artifacts(tmp_path):
    backhaul_input = tmp_path / "backhaul.json"
    backhaul_output = tmp_path / "backhaul-plan.json"
    backhaul_input.write_text(json.dumps({
        "schema": "optimizer.backhaul-observations.v1",
        "observed_at": NOW,
        "root": GW,
        "nodes": [GW, A1],
        "edges": [edge(GW, A1, "5", 30).__dict__],
    }))
    assert main([
        "backhaul-plan", "--input", str(backhaul_input),
        "--output", str(backhaul_output),
    ]) == 0
    assert json.loads(backhaul_output.read_text())["status"] == "recommend"

    width_input = tmp_path / "width.json"
    width_output = tmp_path / "width-plan.json"
    radio = environment("6", 80, (20, 40, 80, 160), 20, 10, 10)
    width_input.write_text(json.dumps({
        "schema": "optimizer.radio-environment.v1",
        "observed_at": NOW,
        "radios": [radio.__dict__],
    }))
    assert main([
        "width-plan", "--input", str(width_input),
        "--output", str(width_output),
    ]) == 0
    recommendation = json.loads(width_output.read_text())["recommendations"][0]
    assert recommendation["action"] == "recommend"
    assert recommendation["target_width_mhz"] == 160
