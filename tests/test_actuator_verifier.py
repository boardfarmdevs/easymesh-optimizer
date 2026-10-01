from __future__ import annotations

import subprocess

import pytest

from optimizer.actuator import SteerActuator
from optimizer.policy import PolicyConfig, ThresholdPolicy
from optimizer.verifier import OutcomeVerifier
from .helpers import STA, TARGET, snapshot


def actionable():
    engine = ThresholdPolicy(PolicyConfig(condition_hold_seconds=0))
    result = engine.evaluate(snapshot(0))
    return result.decisions[0]


def test_actuator_validates_and_executes_exactly_one_narrow_command():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "accepted\n", "")

    result = SteerActuator("/repo/gen/steer.sh", runner=runner).execute(
        actionable(), snapshot(0)
    )
    assert result.success
    assert result.command == ("/repo/gen/steer.sh", STA, TARGET)
    assert len(calls) == 1


def test_actuator_refuses_changed_source():
    with pytest.raises(ValueError, match="source changed"):
        SteerActuator("/repo/gen/steer.sh").execute(actionable(), snapshot(1, source=TARGET))


def test_interactive_preview_is_nonblocking_without_changing_default_environment():
    calls = []
    def runner(command, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(command, 0, "accepted", "")
    SteerActuator("/repo/gen/steer.sh", preview_seconds=0, runner=runner).execute(actionable(), snapshot(0))
    assert calls[-1]['env']['EASYMESH_STEERING_PREVIEW_SECONDS'] == '0'
    SteerActuator("/repo/gen/steer.sh", runner=runner).execute(actionable(), snapshot(0))
    assert 'env' not in calls[-1]
    with pytest.raises(ValueError):
        SteerActuator("/repo/gen/steer.sh", preview_seconds=-1)


def test_narrow_association_verification_still_requires_traffic():
    for traffic_ok in (True, False):
        ticks = iter([0.0, 0.1, 0.2])
        verifier = OutcomeVerifier(None, association_probe=lambda station: TARGET if station == STA else None,
                                   traffic_probe=lambda station: traffic_ok, monotonic=lambda: next(ticks))
        result = verifier.verify(STA, TARGET, timeout_seconds=10, poll_seconds=0.2)
        assert result.success == traffic_ok
        assert result.polls == 1
        assert result.traffic_ok == traffic_ok


def test_request_only_actuator_does_not_compete_with_scenario_rf_writer():
    actuator = SteerActuator("/repo/gen/steer.sh", request_only=True)
    assert actuator.build_command(STA, TARGET) == (
        "/repo/gen/steer.sh", "--request-only", STA, TARGET
    )


def test_verifier_requires_observed_target_and_traffic():
    class Observer:
        values = iter([snapshot(0), snapshot(1, source=TARGET)])

        def observe(self):
            return next(self.values)

    ticks = iter([0.0, 0.1, 1.0, 1.1])
    verifier = OutcomeVerifier(
        Observer(),
        traffic_probe=lambda sta: sta == STA,
        sleeper=lambda value: None,
        monotonic=lambda: next(ticks),
    )
    result = verifier.verify(STA, TARGET, timeout_seconds=10)
    assert result.success
    assert result.polls == 2
    assert result.traffic_ok is True
