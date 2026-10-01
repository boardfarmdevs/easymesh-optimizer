import datetime as dt
import copy
import importlib.util
import io
import json
from pathlib import Path
import pytest


def readiness():
    specification = importlib.util.spec_from_file_location(
        "readiness", Path(__file__).with_name("room-final-readiness.py"))
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.fixture
def room_state():
    return {
        "fleet": {"converged": True, "clients_with_stronger_ap": 1,
                  "stronger_candidates": [{"sta_mac": "aa", "gain_rcpi": 2}]},
        "client_decisions": [{"sta_mac": "AA", "source_bssid": "BB", "current_rcpi": 104,
                             "current_band": "5", "scores": [{"band": "5", "gain_rcpi": 2}]}],
    }, [{"sta_mac": "aa", "connected_bssid": "bb"}]


def test_margin_held_client_is_policy_converged_not_absolute_best(room_state):
    optimizer, clients = room_state
    result = readiness().convergence_state(optimizer, clients)
    assert result["policy_converged"] is True
    assert result["absolute_best_converged"] is False
    assert result["stronger_candidates"] == optimizer["fleet"]["stronger_candidates"]


def test_actionable_client_is_not_converged(room_state):
    optimizer, clients = room_state
    optimizer["fleet"]["converged"] = False
    assert readiness().convergence_state(optimizer, clients)["policy_converged"] is False


@pytest.mark.parametrize("failure", ["missing", "duplicate", "stale_owner", "nan", "bool"])
def test_invalid_decision_coverage_cannot_pass(room_state, failure):
    optimizer, clients = copy.deepcopy(room_state)
    decisions = optimizer["client_decisions"]
    if failure == "missing":
        decisions.clear()
    elif failure == "duplicate":
        decisions.append(decisions[0])
    elif failure == "stale_owner":
        decisions[0]["source_bssid"] = "cc"
    else:
        decisions[0]["current_rcpi"] = float("nan") if failure == "nan" else True
    result = readiness().convergence_state(optimizer, clients)
    assert result["policy_converged"] is False
    assert result["absolute_best_converged"] is False


def test_absolute_best_requires_consistent_scores_and_fleet(room_state):
    optimizer, clients = room_state
    optimizer["client_decisions"][0]["scores"][0]["gain_rcpi"] = 0
    assert readiness().convergence_state(optimizer, clients)["absolute_best_converged"] is False
    optimizer["fleet"]["clients_with_stronger_ap"] = 0
    assert readiness().convergence_state(optimizer, clients)["absolute_best_converged"] is True
    optimizer["fleet"]["converged"] = False
    assert readiness().convergence_state(optimizer, clients)["absolute_best_converged"] is False


@pytest.mark.parametrize("optimizer, expected", [
    ({"maximum_actions": None, "steering_safety": {"enabled": True}}, True),
    ({"maximum_actions": 100, "steering_safety": {"enabled": True}}, False),
    ({"steering_safety": {"enabled": True}}, False),
    ({"maximum_actions": None}, False),
    ({"maximum_actions": None, "steering_safety": {"enabled": False}}, False),
])
def test_default_steering_is_unlimited_with_safety_guards(optimizer, expected):
    assert readiness().default_steering_budget(optimizer) is expected


def test_native_nanoseconds_work_on_python_310():
    path = Path(__file__).with_name("room-final-readiness.py")
    specification = importlib.util.spec_from_file_location("readiness", path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    assert module.parse_timestamp("2026-09-09T21:33:19.167399448Z") == dt.datetime(
        2026, 9, 9, 21, 33, 19, 167399, tzinfo=dt.timezone.utc)
    assert module.parse_timestamp("2026-09-09T21:33:19Z").microsecond == 0


@pytest.mark.parametrize("fault", ["", "dormant_online", "missing_mesh", "small_pool",
                                  "duplicate", "wrong_role", "wrong_room"])
def test_default_room_has_twenty_online_and_eighty_dormant(fault):
    stations = [f"sta_{kind}_{ordinal:02d}" for kind in ("mobile", "static")
                for ordinal in range(1, 11)]
    online = stations + ["gateway", *(f"extender_{ordinal}" for ordinal in range(1, 5))]
    roles = {role: {"present": True} for role in online}
    roles.update({f"sta_pool_{ordinal:03d}": {"present": False} for ordinal in range(21, 101)})
    state = {"roles": roles, "pool_clients": 100, "expected_online_clients": 20,
             "selected_world": "home-five-agent--private-client-room-walk"}
    clients = [{"role": role, "sta_mac": f"02:00:00:00:00:{ordinal:02x}"}
               for ordinal, role in enumerate(stations)]
    if fault == "dormant_online":
        roles["sta_pool_100"]["present"] = True
    elif fault == "missing_mesh":
        roles["extender_1"]["present"] = False
    elif fault == "small_pool":
        state["pool_clients"] = 20
    elif fault == "duplicate":
        clients[-1]["sta_mac"] = clients[0]["sta_mac"]
    elif fault == "wrong_role":
        clients[-1]["role"] = "sta_pool_100"
    elif fault == "wrong_room":
        state["selected_world"] = "home-five-agent--stationary"
    assert readiness().default_roster({"api_total": 20}, clients, state) == (not fault)


@pytest.mark.parametrize("world, extra, expected", [
    ("home-five-agent-pods--private-client-room-walk", ["pod_1", "pod_2", "extender_5"], True),
    ("home-five-agent-pods--private-client-room-walk", ["pod_1", "pod_2"], False),
    ("home-five-agent-wired--private-client-room-walk", ["extender_5"], True),
    ("home-five-agent-wired--private-client-room-walk", [], False),
    ("home-five-agent--private-client-room-walk", ["extender_5"], False),
    ("home-five-agent-shifted--private-client-room-walk", [], False),
])
def test_the_wired_extender_variant_has_one_more_mesh_role(world, extra, expected):
    stations = [f"sta_{kind}_{ordinal:02d}" for kind in ("mobile", "static")
                for ordinal in range(1, 11)]
    online = stations + ["gateway", *(f"extender_{ordinal}" for ordinal in range(1, 5)), *extra]
    roles = {role: {"present": True} for role in online}
    roles.update({f"sta_pool_{ordinal:03d}": {"present": False} for ordinal in range(21, 101)})
    state = {"roles": roles, "pool_clients": 100, "expected_online_clients": 20,
             "selected_world": world}
    clients = [{"role": role, "sta_mac": f"02:00:00:00:00:{ordinal:02x}"}
               for ordinal, role in enumerate(stations)]
    assert readiness().default_roster({"api_total": 20}, clients, state) is expected


@pytest.mark.parametrize("timestamp", [None, 3, "", "invalid", "2026-09-17T00:00:00"])
def test_invalid_metric_timestamp_is_explicitly_not_fresh(timestamp):
    module = readiness()
    now = dt.datetime(2026, 9, 17, tzinfo=dt.timezone.utc)
    clients = [{"sta_mac": str(ordinal), "rcpi": 100, "metric_observed_at": now.isoformat()}
               for ordinal in range(20)]
    clients[3]["metric_observed_at"] = timestamp
    result = module.freshness_state({"evaluated_at": now.isoformat()}, clients, now)
    assert result["fresh"] is False
    assert result["timestamp_errors"][0]["field"] == "client[3].metric_observed_at"


@pytest.mark.parametrize("age,expected", [(-1, False), (0, True), (30, True), (31, False)])
def test_freshness_boundaries_are_unchanged(age, expected):
    module = readiness()
    now = dt.datetime(2026, 9, 17, tzinfo=dt.timezone.utc)
    timestamp = (now - dt.timedelta(seconds=age)).isoformat()
    clients = [{"rcpi": 100, "metric_observed_at": timestamp} for ordinal in range(20)]
    assert module.freshness_state({"evaluated_at": timestamp}, clients, now)["fresh"] is expected
    assert module.freshness_state({"evaluated_at": timestamp}, clients[:-1], now)["fresh"] is False


def test_failed_timestamp_sample_still_preserves_fetched_states(tmp_path, monkeypatch):
    module = readiness()
    current = {"environment_epoch": 1, "optimizer": {},
               "network": {"clients": [{"sta_mac": "aa", "metric_observed_at": None}]}}
    interactions = {"environment_epoch": 1, "playback": {"status": "paused", "time_ms": 0},
                    "lease": {"held": False}, "fault": None}
    responses = iter([current, interactions])
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *args, **kwargs: io.StringIO(json.dumps(next(responses))))
    monotonic = iter([0, 0.5, 2, 3])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(module.time, "sleep", lambda duration: None)
    output = tmp_path / "failed"
    monkeypatch.setattr("sys.argv", ["readiness", "--room-url", "http://example.invalid/",
                                     "--output", str(output), "--timeout", "1"])
    assert module.main() == 1
    assert json.loads((output / "current.json").read_text()) == current
    assert json.loads((output / "interactions.json").read_text()) == interactions
    sample = json.loads((output / "samples.jsonl").read_text())
    assert sample["checks"]["fresh"] is False
    assert {error["field"] for error in sample["timestamp_errors"]} == {
        "optimizer.evaluated_at", "client[aa].metric_observed_at"}
