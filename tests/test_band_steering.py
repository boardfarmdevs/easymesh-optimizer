from concurrent.futures import Future
from dataclasses import replace
from unittest.mock import Mock

import pytest

from optimizer.band_scan import SOURCE as SCAN_SOURCE
from optimizer.band_steering import BandSteeringMeasurements, band_policy, evaluate_band_clients, scan_eligibility
from optimizer.policy import PolicyConfig, ThresholdPolicy
from .helpers import STA, SOURCE, TARGET, snapshot


PROFILE = {"container": "prpl-client-01", "profile": {"allowed_bands": ["2.4", "5", "6"], "initial_band": "2.4"},
           "frequencies_mhz": [2437, 5180, 5975], "settings": {"key_mgmt": "WPA-PSK SAE", "ieee80211w": "1"},
           "capabilities": {"sta_mac": STA, "frequencies_mhz": [2437, 5180, 5975], "key_management": ["WPA-PSK", "SAE"]}}


def received(bssid, band, rcpi, stamp):
    return {"bssid": bssid, "band": band, "frequency_mhz": {"2.4": 2437, "5": 5180, "6": 5975}[band],
            "rcpi": rcpi, "observed_at": stamp, "ssid": "private_ssid", "key_management": ["PSK", "SAE"],
            "pmf_capable": True, "pmf_required": band == "6"}


def sample(seconds=0, **kwargs):
    value = snapshot(seconds, **kwargs)
    return replace(value, clients=(replace(value.clients[0], ssid="private_ssid", cohort="private", measurement_source=SCAN_SOURCE),),
                   candidates=tuple(replace(item, measurement_source=SCAN_SOURCE) for item in value.candidates))


@pytest.mark.parametrize("current,target", [("2.4", "5"), ("5", "6")])
def test_upgrade_prefers_safe_higher_band_without_requiring_rssi_gain(current, target):
    policy = band_policy(PolicyConfig())
    first = policy.evaluate(sample(current_band=current, target_band=target, current_rcpi=140, target_rcpi=132))
    assert first.decisions[0].reason == "condition_hold_not_met"
    final = policy.evaluate(sample(1, current_band=current, target_band=target, current_rcpi=140, target_rcpi=132), first.state)
    assert final.decisions[0].action == "steer" and final.decisions[0].target_band == target
    assert final.decisions[0].reason == "band_preference_hold_satisfied"


def test_edge_fallback_and_band_preference_hysteresis():
    policy = band_policy(PolicyConfig())
    first = policy.evaluate(sample(current_band="5", target_band="2.4", current_rcpi=94, target_rcpi=106))
    result = policy.evaluate(sample(1, current_band="5", target_band="2.4", current_rcpi=94, target_rcpi=106), first.state)
    assert result.decisions[0].action == "steer" and result.decisions[0].target_band == "2.4"
    for seconds in range(10):
        assert policy.evaluate(sample(seconds, current_band="2.4", target_band="5", current_rcpi=106, target_rcpi=94)).decisions[0].action == "none"


def test_band_hold_requires_another_fresh_scan_and_matching_measurement_direction():
    policy = band_policy(PolicyConfig())
    first = policy.evaluate(sample(current_band="2.4", target_band="5", current_rcpi=140, target_rcpi=132))
    reused = sample(1, current_band="2.4", target_band="5", current_rcpi=140, target_rcpi=132, metric_age=1, target_age=1)
    assert policy.evaluate(reused, first.state).decisions[0].reason == "band_waiting_for_new_scan"
    current = sample(1, current_band="2.4", target_band="5", current_rcpi=140, target_rcpi=132)
    mixed = replace(current, clients=(replace(current.clients[0], measurement_source="associated_sta_link_metrics"),))
    assert policy.evaluate(mixed, first.state).decisions[0].reason == "band_measurement_direction_mismatch"


@pytest.mark.parametrize("target_rcpi", [119, 123])
def test_weak_upgrade_or_excessive_loss_is_not_steered(target_rcpi):
    result = band_policy(PolicyConfig()).evaluate(sample(current_band="2.4", target_band="5", current_rcpi=140, target_rcpi=target_rcpi))
    assert result.decisions[0].reason == "no_safe_band_upgrade"


def test_existing_rooms_use_identical_policy_and_band_profile_does_not_modify_others():
    policy = ThresholdPolicy(PolicyConfig(current_rcpi_below=220, condition_hold_seconds=0, minimum_dwell_seconds=0))
    value = sample(current_band="2.4", target_band="5", current_rcpi=140, target_rcpi=132)
    assert evaluate_band_clients(policy, value, None, set()) == policy.evaluate(value)
    assert evaluate_band_clients(policy, value, None, {STA}).decisions[0].reason == "condition_hold_not_met"
    assert policy.config.current_rcpi_below == 220


@pytest.mark.parametrize("change,reason", [({"ssid": "iot_ssid"}, "different_ssid"),
    ({"frequency_mhz": 6135}, "unsupported_or_disallowed_band"),
    ({"key_management": ["PSK"]}, "six_ghz_requires_sae_and_pmf"),
    ({"pmf_required": False}, "six_ghz_requires_sae_and_pmf")])
def test_candidate_security_and_frequency_fail_closed(change, reason):
    target = {**received(TARGET, "6", 140, sample().observed_at), **change}
    assert scan_eligibility(target, PROFILE, "private_ssid") == reason


def measurements():
    future = Future()
    executor = Mock()
    executor.submit.return_value = future
    collector = BandSteeringMeasurements(executor=executor, clock=lambda: 0)
    return collector, future, executor


def scan_result(value):
    return {"scan_id": "scan-1", "elapsed_ms": 180, "samples": {
        SOURCE: received(SOURCE, "5", 140, value.observed_at),
        TARGET: received(TARGET, "6", 132, value.observed_at)}}


def test_scan_publication_is_nonblocking_and_uses_same_direction_for_serving_and_target():
    collector, future, executor = measurements()
    value = sample(target_band="6")
    waiting = collector.enrich(value, {STA: PROFILE}, "world-1")
    collector.schedule()
    assert waiting.clients[0].rcpi is None
    future.set_result(scan_result(value))
    ready = collector.enrich(value, {STA: PROFILE}, "world-1")
    assert ready.clients[0].rcpi == 140 and ready.candidates[0].rcpi == 132
    assert ready.clients[0].measurement_source == ready.candidates[0].measurement_source == SCAN_SOURCE
    assert collector.selected == {STA} and collector.status[STA]["available"]
    assert executor.submit.call_count == 1


@pytest.mark.parametrize("change", ["world", "association", "stale", "future"])
def test_scan_cannot_cross_world_owner_or_freshness_boundaries(change):
    collector, future, _executor = measurements()
    value = sample(target_band="6")
    collector.enrich(value, {STA: PROFILE}, "world-1")
    collector.schedule()
    future.set_result(scan_result(sample(10) if change == "future" else value))
    modified = (sample(3, target_band="6") if change == "stale" else
                sample(source=TARGET, target_band="6") if change == "association" else value)
    result = collector.enrich(modified, {STA: PROFILE}, "world-2" if change == "world" else "world-1")
    assert result.clients[0].rcpi is None
    assert not collector.status[STA]["available"]


def test_unreceived_or_unsupported_candidate_is_explicitly_ineligible_not_zero_signal():
    collector, future, _executor = measurements()
    value = sample(target_band="6")
    collector.enrich(value, {STA: PROFILE}, "world")
    collector.schedule()
    result = scan_result(value)
    del result["samples"][TARGET]
    future.set_result(result)
    enriched = collector.enrich(value, {STA: PROFILE}, "world")
    assert enriched.candidates[0].rcpi is None and not enriched.candidates[0].eligible
    assert collector.status[STA]["rejected"][TARGET] == "not_received_in_fresh_scan"


def test_scan_scheduling_yields_to_native_steering_instead_of_interrupting_authentication():
    collector, _future, executor = measurements()
    collector.enrich(sample(), {STA: PROFILE}, "world")
    collector.schedule({STA})
    executor.submit.assert_not_called()
    collector.schedule()
    assert collector.in_flight(STA)

def test_received_same_band_opt_in_does_not_enable_band_preference_or_matrix_fallback():
    from optimizer.band_steering import received_scan_enabled
    settings = {**PROFILE, "profile": {"allowed_bands": ["5"], "initial_band": "5",
                                     "measurement_mode": "received_same_band"}}
    assert received_scan_enabled(settings)
    assert not received_scan_enabled({**settings, "profile": {"allowed_bands": ["5"], "initial_band": "5"}})
    assert not received_scan_enabled({})
    policy = ThresholdPolicy(PolicyConfig())
    first = evaluate_band_clients(policy, sample(current_rcpi=80, target_rcpi=132, target_band="5"),
                                  None, {STA}, {STA: settings})
    final = evaluate_band_clients(policy, sample(1, current_rcpi=80, target_rcpi=132, target_band="5"),
                                  first.state, {STA}, {STA: settings})
    assert final.decisions[0].action == "steer" and final.decisions[0].target_band == "5"
    assert not band_policy(policy.config, same_band=True).config.band_upgrade_enabled
    assert band_policy(policy.config, same_band=True).config.minimum_target_gain_rcpi == policy.config.minimum_target_gain_rcpi


def test_received_same_band_hold_requires_new_received_samples():
    policy = band_policy(PolicyConfig(), same_band=True)
    first = policy.evaluate(sample(current_rcpi=80, target_rcpi=132, target_band="5"))
    cached = sample(1, current_rcpi=80, target_rcpi=132, target_band="5", metric_age=1, target_age=1)
    assert policy.evaluate(cached, first.state).decisions[0].reason == "band_waiting_for_new_scan"
    mixed = sample(1, current_rcpi=80, target_rcpi=132, target_band="5")
    mixed = replace(mixed, candidates=tuple(replace(item, measurement_source="hal_matrix") for item in mixed.candidates))
    assert policy.evaluate(mixed, first.state).decisions[0].reason == "band_measurement_direction_mismatch"


def test_missing_serving_sample_fails_closed_even_when_other_bss_was_received():
    collector, future, _executor = measurements()
    value = sample(target_band="6")
    collector.enrich(value, {STA: PROFILE}, "world")
    collector.schedule()
    result = scan_result(value)
    del result["samples"][SOURCE]
    future.set_result(result)
    missing = collector.enrich(value, {STA: PROFILE}, "world")
    assert missing.clients[0].rcpi is None
    assert not collector.status[STA]["available"]
