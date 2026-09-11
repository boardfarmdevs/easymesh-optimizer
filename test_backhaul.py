from __future__ import annotations

import itertools
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from room_demo.backhaul import BackhaulManager, RdkBackhaulAdapter, path_quality, select_parent
from room_demo.conductor import LiveConductor
from wmdcfg.geometry import directed_link


def links_for(strengths):
    return [{"source_role": source, "destination_role": destination, "band": "5", "snr_db": value}
            for (source, destination), value in strengths.items()]


def symmetric(pairs):
    return {(source, destination): value for pair, value in pairs.items()
            for source, destination in (pair, pair[::-1])}


def test_centered_gateway_stays_star():
    parents = {"left": "gateway", "right": "gateway"}
    strengths = symmetric({("gateway", "left"): 24, ("gateway", "right"): 24, ("left", "right"): 23})
    assert select_parent(parents, links_for(strengths)) is None


def canonical_strengths(layout_name="home-five-agent", positions=None):
    root = Path(__file__).resolve().parents[2] / "wmediumd/configurator/worlds/layouts"
    layout = json.loads((root / (layout_name + ".json")).read_text())
    nodes = [node for node in layout["nodes"] if node["kind"] == "fronthaul_ap"]
    positions = positions or {node["role"]: node["position"] for node in nodes}
    present = dict.fromkeys(positions, True)
    return {(source["role"], destination["role"]): directed_link(
        source, destination, positions, present, layout, {"seed": 0}, 0, "backhaul"
    )["snr_db_by_band"]["5"] for source in nodes for destination in nodes if source != destination}


def test_real_default_layout_star_beats_requested_branches_and_every_rooted_tree():
    strengths = canonical_strengths()
    parents = {f"extender_{number}": "gateway" for number in range(1, 5)}
    assert all(strengths[(role, "gateway")] == 24 for role in parents)
    branches = {**parents, "extender_4": "extender_2", "extender_3": "extender_1"}
    for child in ("extender_4", "extender_3"):
        assert strengths[(child, branches[child])] == 23
        assert path_quality(child, branches, strengths) == 20
    assert select_parent(parents, links_for(strengths)) is None
    valid_trees = 0
    for candidates in itertools.product(*[sorted({"gateway", *parents} - {child}) for child in parents]):
        candidate = dict(zip(parents, candidates))
        scores = [path_quality(role, candidate, strengths) for role in parents]
        if None in scores:
            continue
        valid_trees += 1
        assert sum(scores) <= 96
        if sum(scores) == 96:
            assert candidate == parents
    assert valid_trees == 125


@pytest.mark.parametrize("layout_name,positions,expected", [
    ("home-five-agent-shifted", None, {"extender_1": "gateway", "extender_2": "extender_1",
                                       "extender_3": "gateway", "extender_4": "extender_3"}),
    ("home-five-agent", {"gateway": [1, 1], "extender_1": [5, 1], "extender_2": [8, 1],
                         "extender_3": [12, 1], "extender_4": [18, 1]},
     {"extender_1": "gateway", "extender_2": "extender_1", "extender_3": "extender_2", "extender_4": "extender_3"}),
])
def test_real_room_geometry_can_select_branches_and_chains(layout_name, positions, expected):
    strengths = canonical_strengths(layout_name, positions)
    parents = {role: "gateway" for role in expected}
    for attempt in range(10):
        choice = select_parent(parents, links_for(strengths))
        if choice is None:
            break
        parents[choice["child"]] = choice["parent"]
    assert parents == expected


def test_corridor_converges_to_chain_without_loops():
    positions = {"gateway": 0, "first": 1, "second": 2, "third": 3, "fourth": 4}
    strengths = {(source, destination): 40 - 8 * abs(positions[source] - positions[destination])
                 for source in positions for destination in positions if source != destination}
    parents = {role: "gateway" for role in positions if role != "gateway"}
    for attempt in range(10):
        choice = select_parent(parents, links_for(strengths))
        if choice is None:
            break
        parents[choice["child"]] = choice["parent"]
        assert all(path_quality(role, parents, strengths) is not None for role in parents)
    assert parents == {"first": "gateway", "second": "first", "third": "second", "fourth": "third"}
    star = {(source, destination): 38 if "gateway" in (source, destination) else 20 for source, destination in strengths}
    for attempt in range(10):
        choice = select_parent(parents, links_for(star))
        if choice is None:
            break
        parents[choice["child"]] = choice["parent"]
    assert set(parents.values()) == {"gateway"}


def test_hysteresis_and_missing_reverse_link():
    parents = {"first": "gateway", "second": "gateway"}
    strengths = symmetric({("gateway", "first"): 35, ("gateway", "second"): 30, ("first", "second"): 34})
    assert select_parent(parents, links_for(strengths)) is None
    strengths[("gateway", "second")] = strengths[("second", "gateway")] = 20
    assert select_parent(parents, links_for(strengths))["parent"] == "first"
    del strengths[("second", "first")]
    assert select_parent(parents, links_for(strengths)) is None
    assert path_quality("first", {"first": "second", "second": "first"}, strengths) is None


def test_two_db_relay_improvement_converges_without_switching_back():
    parents = {"near": "gateway", "far": "gateway"}
    strengths = symmetric({("gateway", "near"): 25, ("gateway", "far"): 20, ("near", "far"): 31})
    choice = select_parent(parents, links_for(strengths))
    assert choice["child"] == "far" and choice["parent"] == "near"
    assert choice["path_score_before"] == 20 and choice["path_score_after"] == 22
    assert choice["gain_db"] == 2
    parents[choice["child"]] = choice["parent"]
    for repeat in range(10):
        assert select_parent(parents, links_for(strengths)) is None
    strengths[("gateway", "far")] = strengths[("far", "gateway")] = 23
    assert select_parent(parents, links_for(strengths)) is None


def manager_fixture():
    adapter = Mock()
    adapter.observe.return_value = {"first": "gateway", "second": "gateway"}
    room = {"environment_epoch": 7, "stable_for_seconds": 10, "movement_active": False,
            "backhaul_links": links_for(symmetric({("gateway", "first"): 35,
                                                   ("gateway", "second"): 20, ("first", "second"): 34}))}
    transaction = Mock(side_effect=lambda action, **kwargs: action())
    manager = BackhaulManager(adapter, transaction, Mock(), clock=Mock(return_value=100))
    return manager, adapter, transaction, room


def test_manager_settle_cooldown_and_epoch_guard():
    manager, adapter, transaction, room = manager_fixture()
    assert not manager.reconcile({**room, "movement_active": True, "stable_for_seconds": 1}, 0)
    adapter.observe.assert_not_called()
    assert manager.reconcile(room, 0)
    assert transaction.call_args.kwargs == {"expected_epoch": 7}
    assert manager.snapshot()["status"] == "verified"
    assert manager.snapshot()["minimum_tree_gain_db_per_change"] == 2
    assert not manager.blocks_client_measurement(room)
    assert adapter.switch.call_count == 1


def test_manager_failures_back_off_and_pause():
    manager, adapter, transaction, room = manager_fixture()
    adapter.switch.side_effect = RuntimeError("failure; previous parent restored")
    for attempt in range(3):
        manager.clock.return_value = 100 + attempt * 61
        assert manager.reconcile(room, 0)
    assert manager.snapshot()["status"] == "paused"
    manager.clock.return_value = 900
    assert not manager.reconcile(room, 0)
    assert adapter.switch.call_count == 3


def test_client_measurement_refresh_does_not_block_backhaul_or_clients():
    manager, adapter, transaction, room = manager_fixture()
    room.update(backhaul_epoch=3, backhaul_stable_for_seconds=50)
    with patch("room_demo.backhaul.select_parent", return_value=None):
        assert not manager.reconcile(room, 0)
        room.update(environment_epoch=8, stable_for_seconds=0)
        assert not manager.blocks_client_measurement(room)
        manager.clock.return_value += 31
        assert not manager.reconcile(room, 0)
        assert not manager.blocks_client_measurement(room)
    assert adapter.observe.call_count == 2
    transaction.assert_not_called()


def test_new_backhaul_epoch_does_not_wait_for_old_periodic_poll():
    manager, adapter, transaction, room = manager_fixture()
    with patch("room_demo.backhaul.select_parent", return_value=None):
        assert not manager.reconcile(room, 0)
        room.update(environment_epoch=8, stable_for_seconds=1)
        assert not manager.blocks_client_measurement(room)
        assert not manager.reconcile(room, 0)
        room["stable_for_seconds"] = 2
        assert not manager.reconcile(room, 0)
    assert adapter.observe.call_count == 2
    assert not manager.blocks_client_measurement(room)
    transaction.assert_not_called()


def adapter_fixture():
    adapter = object.__new__(RdkBackhaulAdapter)
    adapter.bssids = {"gateway": "02:00:00:00:00:01", "first": "02:00:00:00:00:02"}
    adapter.observe = Mock(return_value={"first": "gateway", "second": "gateway"})
    adapter.command = Mock(return_value="ssid mesh_backhaul")
    adapter.set_bssid = Mock()
    adapter.verify = Mock()
    return adapter


def test_switch_rollback_and_active_ap_not_reloaded():
    adapter = adapter_fixture()
    adapter.verify.side_effect = [RuntimeError("no traffic"), None]
    with pytest.raises(RuntimeError, match="previous parent restored"):
        adapter.switch({"child": "second", "parent": "first", "previous_parent": "gateway"}, adapter.observe())
    assert adapter.set_bssid.call_args_list[0].args == ("second", adapter.bssids["first"])
    assert adapter.set_bssid.call_args_list[1].args == ("second", adapter.bssids["gateway"])
    assert adapter.command.call_count == 1


def test_switch_stale_graph_and_failed_rollback():
    adapter = adapter_fixture()
    choice = {"child": "second", "parent": "first", "previous_parent": "gateway"}
    with pytest.raises(RuntimeError, match="graph changed"):
        adapter.switch(choice, {})
    adapter.set_bssid.assert_not_called()
    adapter.verify.side_effect = RuntimeError("no traffic")
    with pytest.raises(RuntimeError, match="ROLLBACK FAILED"):
        adapter.switch(choice, adapter.observe())


def test_unsupported_backend_rejected():
    with pytest.raises(ValueError, match="only the fixed"):
        RdkBackhaulAdapter({"bindings": {}})


def test_incomplete_parent_graph_is_not_reported_as_stable():
    with pytest.raises(ValueError, match="incomplete"):
        select_parent({"first": "second", "second": "first"}, [])


def test_verification_allows_association_and_bridge_settling():
    adapter = adapter_fixture()
    observations = []

    def command(role, *arguments, **kwargs):
        if arguments[0] == "iw":
            observations.append(role)
            return "Not connected" if len(observations) <= 10 else f"Connected to {adapter.bssids['first']}"
        return "0% packet loss"

    adapter.command = Mock(side_effect=command)
    with patch("room_demo.backhaul.time.sleep"), patch("room_demo.backhaul.time.monotonic", side_effect=range(100)):
        RdkBackhaulAdapter.verify(adapter, "second", "first")
    assert len(observations) == 12


def test_status_invalidates_old_stable_decision_on_room_change():
    manager, adapter, transaction, room = manager_fixture()
    manager.status = {"status": "stable", "environment_epoch": 7, "reason": "No improvement"}
    assert manager.snapshot(room)["status"] == "stable"
    assert manager.snapshot({**room, "environment_epoch": 8})["status"] == "waiting"
    assert not manager.blocks_client_measurement(room)
    assert not manager.blocks_client_measurement({**room, "environment_epoch": 8})
    manager.status = {"status": "verified"}
    assert not manager.blocks_client_measurement(room)
    manager.status = {"status": "switching"}
    assert manager.blocks_client_measurement(room)
    manager.status = {"status": "retry"}
    assert not manager.blocks_client_measurement(room)


def test_verification_retries_transient_link_query_timeout_within_its_deadline():
    adapter = adapter_fixture()
    adapter.command.side_effect = [
        subprocess.TimeoutExpired(['lxc', 'exec', 'second', '--', 'iw'], 4),
        RuntimeError('temporary container exec failure'),
        f"Connected to {adapter.bssids['first']}", '0% packet loss',
        f"Connected to {adapter.bssids['first']}", '0% packet loss',
    ]
    with patch('room_demo.backhaul.time.sleep'), patch('room_demo.backhaul.time.monotonic', side_effect=range(100)):
        RdkBackhaulAdapter.verify(adapter, 'second', 'first')
    assert adapter.command.call_count == 6


def test_applied_snr_matches_actual_parent_band_and_channel_only():
    conductor = object.__new__(LiveConductor)
    conductor.backhaul_manager = None
    conductor._traffic_probe = lambda room: {"sta_mac": "unused"}
    edge = {"parent_role": "gateway", "child_role": "first", "band": "5", "channel": 36}
    conductor._topology_payload = lambda topology: {"backhaul_edges": [dict(edge)]}
    snapshot = SimpleNamespace(clients=[], observed_at="now", health=SimpleNamespace(devices=5, clients=0, bsses=45))
    link = {"source_role": "gateway", "destination_role": "first", "band": "5",
            "snr_db": 24, "frequency_mhz": 5180, "source": "wmediumd_verified_applied_rf"}
    room = {"backhaul_links": [link]}
    assert conductor._network_payload(snapshot, room=room)["mesh"]["backhaul_edges"][0]["applied_rf"] == link
    for changed in ({"channel": 40}, {"parent_role": "second"}, {"band": "6"}):
        original = dict(edge)
        edge.update(changed)
        assert "applied_rf" not in conductor._network_payload(snapshot, room=room)["mesh"]["backhaul_edges"][0]
        edge.update(original)
