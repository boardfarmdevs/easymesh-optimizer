from copy import deepcopy
from datetime import timedelta

import pytest

from optimizer.backhaul_observations import backhaul_observations
from optimizer.model import parse_time


STAMP = "2026-09-21T12:00:00Z"


def fixture():
    network = {"observed_at": STAMP, "mesh": {"available": True, "source": "controller_topology_api",
        "nodes": [{"role": role, "device_id": role} for role in ("gateway", "extender_1", "extender_2")],
        "backhaul_edges": [
            {"child_role": "extender_1", "parent_role": "gateway", "band": "5", "channel": 36,
             "media_type": "Wireless LAN",
             "upstream_bssid": "ap-0", "signal": {"status": "fresh", "rssi_dbm": -40, "observed_at": STAMP}},
            {"child_role": "extender_2", "parent_role": "extender_1", "band": "5", "channel": 36,
             "media_type": "Wireless LAN",
             "upstream_bssid": "ap-1", "signal": {"status": "fresh", "rssi_dbm": -50}}]}}
    inspection = {"bss_loads": [{"bssid": "ap-0", "device_id": "gateway", "context_state": "verified",
        "frequency_mhz": 5180, "utilization": 0, "observed_at": STAMP, "source": "native_ap_metrics"}]}
    return network, inspection


def test_native_branch_zero_load_and_missing_backhaul_traffic_are_separate():
    network, inspection = fixture()
    original = deepcopy(network)
    result = backhaul_observations(network, inspection, now=parse_time(STAMP))
    first, second = result["paths"]
    assert not result["decision_inputs"]
    assert first["links"][0]["utilization"]["value"] == 0
    assert first["links"][0]["signal"]["value"] == -40
    assert first["links"][0]["traffic"]["value"] is None
    assert second["path"] == ["extender_2", "extender_1", "gateway"]
    assert second["wireless_hops"] == 2 and second["shared_frequency_hops"] == {"5180": 2}
    assert second["links"][0]["signal"]["value"] is None
    assert network == original


@pytest.mark.parametrize("seconds,state", [(6, "stale"), (-1, "invalid")])
def test_stale_or_future_topology_cannot_be_freshened_by_publication(seconds, state):
    network, inspection = fixture()
    result = backhaul_observations(network, inspection, now=parse_time(STAMP) + timedelta(seconds=seconds))
    assert all(row["state"] == state and not row["links"] and not row["path"] for row in result["paths"])


@pytest.mark.parametrize("change", ["cycle", "missing", "duplicate"])
def test_incomplete_or_ambiguous_native_paths_are_unavailable(change):
    network, inspection = fixture()
    edges = network["mesh"]["backhaul_edges"]
    if change == "cycle":
        edges[0]["parent_role"] = "extender_2"
    elif change == "missing":
        edges.pop(0)
    else:
        edges.append(deepcopy(edges[0]))
    result = backhaul_observations(network, inspection, now=parse_time(STAMP))
    assert all(row["state"] == "invalid" and row["wireless_hops"] is None for row in result["paths"])


@pytest.mark.parametrize("field,value", [("context_state", "unverified"), ("frequency_mhz", 5220),
                                        ("device_id", "other"), ("bssid", "other"), ("source", "geometry")])
def test_load_requires_exact_native_parent_context(field, value):
    network, inspection = fixture()
    inspection["bss_loads"][0][field] = value
    result = backhaul_observations(network, inspection, now=parse_time(STAMP))
    assert result["paths"][0]["links"][0]["utilization"]["value"] is None
