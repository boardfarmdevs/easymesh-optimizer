import importlib.util
from pathlib import Path
from unittest.mock import Mock


SCRIPT = Path(__file__).with_name("room-world-switch-smoke.py")
SPEC = importlib.util.spec_from_file_location("world_switch_acceptance", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def current_snapshot(gain=0):
    return {
        "network": {"clients": [{"sta_mac": "client", "connected_bssid": "source"}]},
        "optimizer": {
            "fleet": {"converged": True, "absolute_best_converged": gain <= 0},
            "client_decisions": [{"sta_mac": "client", "source_bssid": "source",
                                  "current_rcpi": 100, "current_band": "5",
                                  "scores": [{"band": "5", "gain_rcpi": gain}]}],
        },
    }


def test_policy_margin_is_reported_separately_from_absolute_best():
    assert MODULE.client_convergence(current_snapshot(2), 1) == (True, False)
    assert MODULE.client_convergence(current_snapshot(), 1) == (True, True)


def test_missing_clients_and_stale_owners_do_not_converge():
    current = current_snapshot()
    assert MODULE.client_convergence(current, 2) == (False, False)
    current["optimizer"]["client_decisions"][0]["source_bssid"] = "old-owner"
    assert MODULE.client_convergence(current, 1) == (False, False)


def test_policy_failure_remains_a_failure():
    current = current_snapshot(20)
    current["optimizer"]["fleet"]["converged"] = False
    assert MODULE.client_convergence(current, 1) == (False, False)


def test_release_uses_only_owned_token():
    request = Mock()
    MODULE.release_control(request, "owned-token")
    request.assert_called_once_with("/api/demo/interactions/lease", {"token": "owned-token"}, method="DELETE")


def test_lease_release_is_in_unconditional_cleanup():
    source = SCRIPT.read_text()
    assert 'finally:\n            if lease is not None:' in source
    assert source.index('release_control(request, lease)') > source.index('report["restore_error"]')
    assert source.index('if report.get("lease_release_error")') > source.rindex('args.output.write_text')


def test_bounded_room_selection_does_not_start_a_lab():
    import subprocess
    import sys
    help_result = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                                 capture_output=True, text=True, check=True, timeout=10)
    assert "--skip-presence" in help_result.stdout and "--world WORLD" in help_result.stdout
    assert "--stack {prpl,rdk}" in help_result.stdout and "--roster-only" in help_result.stdout
    invalid = subprocess.run([sys.executable, str(SCRIPT), "--yes-act", "--world", "default",
                              "--all-worlds", "--output", "/unused.json"],
                             capture_output=True, text=True, timeout=10)
    assert invalid.returncode == 2 and "choose --all-worlds or --world" in invalid.stderr


def test_the_stack_comes_from_the_lab_containers():
    assert MODULE.detect_stack({"bpibroadband", "bpiap", "wlan-client-001"}) == "rdk"
    assert MODULE.detect_stack({"prpl-controller", "prpl-agent-01", "prpl-client-01"}) == "prpl"
    try:
        MODULE.detect_stack({"pod-1"})
    except RuntimeError:
        pass
    else:
        raise AssertionError("a VM without either lab must be refused")


def test_topology_stations_and_nodes_of_both_controllers():
    rdk = {"nodes": [{"STAList": [{"staMAC": "02:00:00:00:00:0A"}]}, {"STAList": None}, {}]}
    assert MODULE.topology_stations("rdk", rdk) == ({"02:00:00:00:00:0a"}, 3)
    prpl = {"devices": [{"radios": [{"bsses": [{"clients": [{"id": "02:00:00:00:00:0B"}]}]}]}, {"radios": []}]}
    assert MODULE.topology_stations("prpl", prpl) == ({"02:00:00:00:00:0b"}, 2)
    # six mesh devices: RDK's topology has the controller as a seventh node, prplMesh's six
    assert {stack: 6 + profile["topology_nodes_beyond_mesh"] for stack, profile in MODULE.STACKS.items()} == \
        {"rdk": 7, "prpl": 6}
