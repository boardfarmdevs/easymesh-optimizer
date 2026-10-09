from __future__ import annotations

from room_service.cli import parser


def test_check_uses_the_stacks_control_socket(monkeypatch):
    monkeypatch.setenv("OPTIMIZER_STACK", "prplmesh")
    args = parser().parse_args(["check"])
    assert args.socket == "/run/prpl-wmediumd/control.sock"
    monkeypatch.setenv("OPTIMIZER_STACK", "rdk")
    assert parser().parse_args(["check"]).socket == "/run/wmediumd-control.sock"


def test_interactive_defaults_are_the_stacks(monkeypatch):
    monkeypatch.setenv("OPTIMIZER_STACK", "prplmesh")
    args = parser().parse_args(["interactive"])
    assert args.base_url == "http://127.0.0.1:8092"
    assert str(args.lock) == "/run/lock/prplmesh-room-service.lock"
    assert str(args.recovery_file) == "/run/prplmesh-room-service/recovery.json"
    monkeypatch.setenv("OPTIMIZER_STACK", "rdk")
    args = parser().parse_args(["interactive"])
    assert args.base_url == "http://127.0.0.1:8888"
    assert str(args.lock) == "/run/lock/easymesh-room-service.lock"
    assert str(args.output_root) == "/tmp/easymesh-room-service-runs"


def test_the_inventory_waits_for_a_node_mid_restart(monkeypatch):
    # rdk-1004, 9 October: an extender short of its bands, then a pod with no AP up, each a
    # service exit until systemd's start limit; now a bounded wait
    from unittest.mock import patch
    from room_service import cli
    from wmdcfg.model import ScenarioError

    answers = [ScenarioError("bpiap-001: expected tri-band radio inventory, found ['5']"),
               ScenarioError("pod-1: no operating AP radio"), {"radios": []}]

    def discover():
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    sleeps = []
    with patch("room_service.cli.discover", side_effect=discover), \
            patch("room_service.cli.time.sleep", side_effect=sleeps.append):
        assert cli._discover_settled(timeout=60, interval=5) == {"radios": []}
    assert sleeps == [5, 5]


def test_any_other_inventory_error_and_a_wait_past_its_bound_fail():
    from unittest.mock import patch
    import pytest
    from room_service import cli
    from wmdcfg.model import ScenarioError

    with patch("room_service.cli.discover", side_effect=ScenarioError("bpiap: phy7 has ambiguous 5GHz frequencies")), \
            patch("room_service.cli.time.sleep") as sleep:
        with pytest.raises(ScenarioError, match="ambiguous"):
            cli._discover_settled(timeout=60)
        sleep.assert_not_called()
    clock = iter([0.0, 10.0, 70.0])
    with patch("room_service.cli.discover", side_effect=ScenarioError("pod-1: no operating AP radio")), \
            patch("room_service.cli.time.sleep"), \
            patch("room_service.cli.time.monotonic", side_effect=lambda: next(clock)):
        with pytest.raises(ScenarioError, match="no operating AP radio"):
            cli._discover_settled(timeout=60)
