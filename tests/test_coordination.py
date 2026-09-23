from dataclasses import replace
from io import BytesIO
import json
import subprocess

import pytest

from optimizer.actuator import NativeSteerActuator
from optimizer.observer import ControllerObserver
from .helpers import STA, TARGET, snapshot
from .test_actuator_verifier import actionable


@pytest.mark.parametrize("payload,expected", [
    ({}, 1),
    ({"candidate_parallel_agents": 5}, 1),
    ({"schema": "easymesh.cli.coordination.v1", "candidate_parallel_agents": 5,
      "candidate_wait_holds_native_lock": False, "native_command_and_tree_ownership_serialized": True}, 5),
    ({"schema": "easymesh.cli.coordination.v1", "candidate_parallel_agents": 100,
      "candidate_wait_holds_native_lock": False, "native_command_and_tree_ownership_serialized": True}, 1),
])
def test_parallelism_requires_complete_safe_capabilities(payload, expected):
    observer = ControllerObserver(fetcher=lambda _url: payload)
    assert observer.coordination_capabilities()["candidate_parallel_agents"] == expected


def test_old_server_keeps_safe_serial_collection():
    def unavailable(_url):
        raise OSError("404")
    assert ControllerObserver(fetcher=unavailable).coordination_capabilities()["candidate_parallel_agents"] == 1


def test_unassisted_actuator_submits_one_gentle_native_request():
    calls = []
    def runner(command, **options):
        calls.append((command, options))
        return subprocess.CompletedProcess(command, 0, 'steer_drv_status=Success\n', "")
    result = NativeSteerActuator({TARGET: 36}, runner=runner).execute(
        replace(actionable(), target_band="5"), snapshot(0))
    assert result.success
    assert result.command == ("lxc", "exec", "bpibroadband", "--", "/usr/bin/steer.sh",
                              STA, TARGET, "115", "36", "gentle", actionable().source_bssid)
    assert len(calls) == 1
    assert calls[0][1]["timeout"] == 15
    assert "env" not in calls[0][1]


@pytest.mark.parametrize("output", ["", 'steer_drv_status=Error_Prev_Cmd_In_Progress\n'])
def test_native_busy_or_missing_status_is_not_a_successful_submission(output):
    actuator = NativeSteerActuator({TARGET: 36}, runner=lambda command, **_options:
                                  subprocess.CompletedProcess(command, 0, output, ""))
    assert not actuator.execute(replace(actionable(), target_band="5"), snapshot(0)).success


@pytest.mark.parametrize("status,accepted,native_status,expected", [(200, True, "Success", True),
    (503, False, "Error_Prev_Cmd_In_Progress", False), (200, True, "", False), (503, True, "Success", False)])
def test_http_submission_requires_http_and_native_acceptance(monkeypatch, status, accepted, native_status, expected):
    calls = []
    def request(command, **options):
        calls.append((command, options))
        response = BytesIO(json.dumps({"success": accepted, "returncode": 0,
            "stdout": "steer_drv_status=" + native_status + "\n" if native_status else ""}).encode())
        response.status = status
        return response
    monkeypatch.setattr("optimizer.actuator.urllib.request.urlopen", request)
    actuator = NativeSteerActuator({TARGET: 36}, base_url="http://controller")
    result = actuator.execute(replace(actionable(), target_band="5"), snapshot(0))
    assert result.success == expected
    assert result.command[:2] == ("POST", "http://controller/api/v1/steer-native")
    assert len(calls) == 1
    payload = json.loads(calls[0][0].data)
    assert payload["source_bssid"] == actionable().source_bssid
    assert payload["operating_class"] == 115 and payload["channel"] == 36


def test_ambiguous_http_outcome_does_not_retry_or_fall_back_to_container_exec(monkeypatch):
    calls = []
    def fail(*arguments, **options):
        calls.append(arguments)
        raise TimeoutError("reply lost")
    monkeypatch.setattr("optimizer.actuator.urllib.request.urlopen", fail)
    monkeypatch.setattr("optimizer.actuator.subprocess.run", lambda *_args, **_options: pytest.fail("duplicate submission"))
    result = NativeSteerActuator({TARGET: 36}, base_url="http://controller").execute(
        replace(actionable(), target_band="5"), snapshot(0))
    assert not result.success and len(calls) == 1
