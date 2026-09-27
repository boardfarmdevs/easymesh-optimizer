import ast
from pathlib import Path
from unittest.mock import Mock

from room_demo.engine import RoomEngine
from room_demo.interactions import InteractiveMediumSession


ROOT = Path(__file__).resolve().parents[2]


def test_traffic_stop_does_not_wait_for_the_medium_actor_or_restore_rf():
    traffic = Mock()
    session = InteractiveMediumSession.__new__(InteractiveMediumSession)
    session._traffic_experiment = traffic
    engine = RoomEngine.__new__(RoomEngine)
    engine._session = session
    engine.stop_traffic()
    traffic.close.assert_called_once_with()


def test_traffic_stop_without_configured_worker_is_safe():
    session = InteractiveMediumSession.__new__(InteractiveMediumSession)
    session._traffic_experiment = None
    session.stop_traffic()


def test_cli_stops_traffic_before_waiting_for_other_workers():
    source = (ROOT / "demo/room_demo/cli.py").read_text()
    tree = ast.parse(source)
    finalizers = [node.finalbody for node in ast.walk(tree) if isinstance(node, ast.Try)
                  and any(isinstance(child, ast.Call) and ast.unparse(child.func) == "interactions.stop_traffic"
                          for statement in node.finalbody for child in ast.walk(statement))]
    assert len(finalizers) == 1
    teardown = ast.unparse(ast.Module(body=finalizers[0], type_ignores=[]))
    assert teardown.index("interactions.stop_traffic()") < teardown.index("server.close()")
    assert teardown.index("interactions.stop_traffic()") < teardown.index("conductor.stop()")


def test_service_signals_owner_before_children_and_retains_kill_deadline():
    services = [ROOT / "vm/scripts/guest/easymesh-room-demo.service",
                ROOT / "deploy/guest/prplmesh-room-demo.service"]
    service = next(path for path in services if path.exists()).read_text()
    assert "KillMode=mixed\n" in service
    assert "TimeoutStopSec=180\n" in service
