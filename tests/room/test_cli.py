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
