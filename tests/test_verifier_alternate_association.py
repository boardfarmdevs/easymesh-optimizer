import pytest

from optimizer.verifier import OutcomeVerifier


STATION = "02:00:00:00:0d:00"
SOURCE = "02:00:00:00:01:00"
TARGET = "02:00:00:00:02:00"
ALTERNATE = "02:00:00:00:03:00"


def verify_sequence(values, *, source=SOURCE, traffic=True):
    clock = [0.0]
    remaining = iter(values)
    current = [None]
    traffic_calls = []

    def observe(station):
        assert station == STATION
        current[0] = next(remaining, current[0])
        return current[0]

    def sleep(seconds):
        clock[0] += seconds

    def probe(station):
        traffic_calls.append(station)
        return traffic

    verifier = OutcomeVerifier(None, association_probe=observe, traffic_probe=probe,
                               monotonic=lambda: clock[0], sleeper=sleep)
    result = verifier.verify(STATION, TARGET, timeout_seconds=40, source_bssid=source)
    return result, traffic_calls


def test_native_alternate_association_ends_pending_attempt_without_waiting_timeout():
    result, traffic_calls = verify_sequence([SOURCE, None, ALTERNATE])
    assert not result.success
    assert result.reason == "associated_with_other_ap"
    assert result.final_bssid == ALTERNATE
    assert result.elapsed_seconds == 2
    assert result.polls == 3
    assert not traffic_calls and result.traffic_ok is None


@pytest.mark.parametrize("values", [[SOURCE], [SOURCE, None], [ALTERNATE], [ALTERNATE, None, ALTERNATE]])
def test_source_only_disconnect_or_unproven_cached_owner_keeps_original_timeout(values):
    result, traffic_calls = verify_sequence(values)
    assert not result.success and result.reason == "association_timeout"
    assert result.elapsed_seconds > 40
    assert not traffic_calls


def test_alternate_can_only_fail_after_source_has_been_observed_in_this_attempt():
    result, _traffic_calls = verify_sequence([ALTERNATE, SOURCE, ALTERNATE])
    assert result.reason == "associated_with_other_ap" and result.polls == 3


@pytest.mark.parametrize("traffic", [True, False])
def test_target_still_requires_traffic_after_transient_absence(traffic):
    result, traffic_calls = verify_sequence([SOURCE, None, TARGET], traffic=traffic)
    assert result.success is traffic
    assert result.reason == ("association_and_traffic_converged" if traffic else "traffic_failed")
    assert traffic_calls == [STATION]
    assert result.elapsed_seconds == 2


def test_unspecified_source_retains_original_wait_for_target():
    result, _traffic_calls = verify_sequence([SOURCE, ALTERNATE, TARGET], source=None)
    assert result.success and result.polls == 3


def test_cached_other_ap_before_target_is_not_reported_as_a_failed_attempt():
    result, _traffic_calls = verify_sequence([ALTERNATE, TARGET])
    assert result.success and result.polls == 2
