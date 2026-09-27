from dataclasses import dataclass, replace
from unittest.mock import Mock, patch
import threading

import pytest

from room_demo.conductor import LiveConductor
from room_demo.events import EventStore


@dataclass(frozen=True)
class Observation:
    observed_at: str = "before"
    bss_loads: tuple = ()
    client_activity: tuple = ()


def conductor(interactive=True):
    result = object.__new__(LiveConductor)
    result.interactive = interactive
    result._load_provider = None
    result._load_error = None
    result._rf_start_lock = threading.Lock()
    return result


def test_interactive_collection_does_not_require_load_policy():
    observer = conductor()
    with patch("room_demo.conductor.NativeLoadProvider") as provider:
        observer._start_rf_observation(False)
    provider.assert_called_once()
    assert observer._load_provider is provider.return_value


def test_noninteractive_signal_session_keeps_collection_opt_in():
    observer = conductor(False)
    with patch("room_demo.conductor.NativeLoadProvider") as provider:
        observer._start_rf_observation(False)
    provider.assert_not_called()


def test_optional_receiver_failure_does_not_stop_signal_steering():
    observer = conductor()
    with patch("room_demo.conductor.NativeLoadProvider", side_effect=RuntimeError("receiver unavailable")):
        observer._start_rf_observation(False)
        with pytest.raises(RuntimeError, match="receiver unavailable"):
            observer._start_rf_observation(True)
    assert observer._load_provider is None
    assert observer._load_error == "receiver unavailable"


def test_observation_snapshot_does_not_replace_policy_measurement_time():
    observer = conductor()
    original = Observation()
    enriched = replace(original, observed_at="after", bss_loads=("native",))
    observer._load_provider = Mock(error=None, enrich=Mock(return_value=enriched))
    assert observer._observe_rf(original, {}, False) is enriched
    assert original.observed_at == "before" and original.bss_loads == ()
    observer._load_provider.enrich.side_effect = ValueError("malformed report")
    assert observer._observe_rf(original, {}, False) == original
    assert observer._load_error == "malformed report"
    with pytest.raises(ValueError, match="malformed report"):
        observer._observe_rf(original, {}, True)


@pytest.mark.parametrize("guard_enabled", [False, True])
def test_independent_worker_coalesces_cache_without_policy_or_controller_calls(tmp_path, guard_enabled):
    observer = conductor()
    observer.store = EventStore("rf-test", {"name": "test", "duration_ms": 1000, "tick_ms": 100},
                                tmp_path / "events.jsonl", persist=False)
    observer._wait_for_run = Mock(return_value=True)
    observer._active = Mock(return_value=True)
    observer._time = Mock(return_value=0)
    observer._ap_role_by_bssid = {}
    observer._rf_policy_enabled = guard_enabled
    observer._rf_counter_guard_enabled = guard_enabled
    observer.stop_event = Mock(is_set=Mock(return_value=False), wait=Mock(side_effect=[False, True]))
    observer._load_provider = Mock(inspection=Mock(return_value={
        "schema": "easymesh.rf-inspection.v2", "enabled": True, "bss_loads": [], "client_activity": []}))
    with patch.object(observer.store, "publish_rf_observations", wraps=observer.store.publish_rf_observations) as publish:
        observer._rf_observer_worker()
    assert observer._load_provider.inspection.call_count == 2
    observer._load_provider.inspection.assert_called_with(include_envelope=False)
    publish.assert_called_once()
    observer._load_provider.enrich.assert_not_called()
    assert observer.store.rf_observations()["policy_enabled"] is guard_enabled
    assert observer.store.rf_observations()["counter_guard_enabled"] is guard_enabled
