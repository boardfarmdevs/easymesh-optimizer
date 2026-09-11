import threading
from unittest.mock import Mock

import pytest
import test_conductor as conductor_fixtures


@pytest.fixture
def conductor_and_store(request):
    fixtures = conductor_fixtures.ConductorProjectionTests()
    request.addfinalizer(fixtures.doCleanups)
    return fixtures._conductor()


def test_profile_observation_survives_motion_but_not_membership_or_world_change(conductor_and_store):
    conductor, store = conductor_and_store
    conductor.profiling = True
    room = {"selected_world": "room", "roles": {"client": {"present": True}},
            "daemon": {"instance_id": "medium", "generation": 1}, "revision": 1,
            "environment_epoch": 1}
    key = conductor._observation_key(room)
    room.update(revision=2, environment_epoch=2)
    room["daemon"]["generation"] = 2
    assert key == conductor._observation_key(room)
    room["roles"]["client"]["present"] = False
    assert key != conductor._observation_key(room)


def test_profile_does_not_probe_or_retimestamp_kernel_metrics(conductor_and_store):
    conductor, _store = conductor_and_store
    conductor.profiling = True
    client = Mock()
    assert conductor._client_link_fallback(client, {}) is client
    assert conductor._current_metric_floor(client, {"last_rf_applied_at": "now"}) is None


def test_stop_wakes_environment_wait_without_polling(conductor_and_store):
    conductor, store = conductor_and_store
    completed = threading.Event()
    waiter = threading.Thread(target=lambda: (store.wait_environment(0, 10, conductor.stop_event), completed.set()))
    waiter.start()
    conductor.stop_event.set()
    store.wake()
    assert completed.wait(1)
    waiter.join()


def test_native_observation_explicitly_disables_external_policy(conductor_and_store):
    conductor, _store = conductor_and_store
    conductor.mode = "stimulus"
    conductor.profiling = True
    contract = conductor.profiling_contract()
    assert contract["mode"] == "native-observation"
    assert not contract["external_candidate_queries"]
    assert not contract["external_client_steering"]
    assert not contract["rf_steering_assistance"]
    assert not contract["native_autonomous_optimizer_claim"]


def test_unassisted_requests_are_not_labeled_native_autonomous_policy(conductor_and_store):
    conductor, _store = conductor_and_store
    conductor.mode = "act"
    conductor.profiling = True
    conductor.steering_transaction = None
    contract = conductor.profiling_contract()
    assert contract["mode"] == "unassisted-btm"
    assert contract["optimizer"] == "external-room-threshold-policy"
    assert contract["external_candidate_queries"] and contract["external_client_steering"]
    assert not contract["rf_steering_assistance"]
