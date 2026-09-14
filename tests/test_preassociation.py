from __future__ import annotations

from datetime import datetime, timedelta, timezone

from optimizer.model import format_time
from optimizer.preassociation import (
    PreAssociationConfig,
    PreAssociationPolicy,
    ProbeObservation,
)


START = datetime(2026, 8, 20, 20, 0, tzinfo=timezone.utc)


def probe(seconds, band="2.4", supported=("2.4", "5", "6")):
    return ProbeObservation(
        sta_mac="02:00:00:00:01:00",
        bssid="02:00:00:aa:bb:cc",
        band=band,
        observed_at=format_time(START + timedelta(seconds=seconds)),
        known_supported_bands=supported,
    )


def test_unknown_higher_band_capability_never_suppresses_24ghz():
    decision, _ = PreAssociationPolicy().evaluate(probe(0, supported=()))
    assert decision.action == "allow_probe_response"
    assert decision.reason == "higher_band_support_unknown"


def test_24ghz_suppression_is_bounded_by_probe_count_and_cooldown():
    policy = PreAssociationPolicy(
        PreAssociationConfig(
            maximum_suppression_seconds=10,
            maximum_suppressed_probes=2,
            failsafe_cooldown_seconds=30,
        )
    )
    first, state = policy.evaluate(probe(0))
    second, state = policy.evaluate(probe(1), state)
    failsafe, state = policy.evaluate(probe(2), state)
    cooldown, _ = policy.evaluate(probe(20), state)
    assert [first.action, second.action] == [
        "suppress_probe_response",
        "suppress_probe_response",
    ]
    assert failsafe.action == "allow_probe_response"
    assert failsafe.reason == "bounded_24ghz_failsafe"
    assert cooldown.reason == "failsafe_cooldown"


def test_preferred_band_probe_immediately_ends_suppression_state():
    policy = PreAssociationPolicy()
    _, state = policy.evaluate(probe(0))
    decision, state = policy.evaluate(probe(1, band="6"), state)
    assert decision.action == "allow_probe_response"
    assert decision.reason == "preferred_band_probe_observed"
    assert state.for_sta(probe(0).sta_mac).suppressed_probes == 0


def test_time_window_also_forces_the_24ghz_failsafe():
    policy = PreAssociationPolicy(
        PreAssociationConfig(maximum_suppression_seconds=3, maximum_suppressed_probes=20)
    )
    _, state = policy.evaluate(probe(0))
    decision, _ = policy.evaluate(probe(3), state)
    assert decision.reason == "bounded_24ghz_failsafe"
