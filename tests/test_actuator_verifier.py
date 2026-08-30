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
