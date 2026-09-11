from __future__ import annotations

import tempfile
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from concurrent.futures import Future, ThreadPoolExecutor
import threading
import unittest
from pathlib import Path
import sys
from unittest.mock import Mock, patch

if sys.version_info < (3, 9):
    raise unittest.SkipTest("optimizer runtime requires Python 3.9 or newer")

from optimizer.model import CandidateObservation, ClientObservation, MeshHealth, Snapshot
from optimizer.candidates import CandidateMetricsError, CandidateMetricsUnavailable, CandidateSnapshotSuperseded
from optimizer.policy import Decision, Evaluation, PolicyConfig, ThresholdPolicy
from optimizer.state import ClientPolicyState, PolicyState
from room_demo.conductor import (
    CANDIDATE_PRIORITY_WINDOW_SECONDS,
    LiveConductor,
    _action_measurements_fresh,
    _candidate_measurement_needed,
    _priority_client,
    _deferred_state,
    _fleet_status,
    _client_optimizer_status,
    _interrupted_measurement_state,
    _interactive_policy,
    _completed_action_state,
    _ranked_action_batch,
    _simulated_bss_channels,
)
from room_demo.events import EventStore


class ConductorProjectionTests(unittest.TestCase):
    def test_full_verification_queue_never_marks_unsent_clients_pending(self):
        conductor, store = self._conductor()
        conductor.interactive = True
        conductor.profiling = True
        conductor.mode = "act"
        conductor.manifest.update({"policy": "policy.yaml", "optimizer": {
            "allow_simulated_candidates": True, "request_only": True, "interval_seconds": 5,
            "action_window_ms": [0, 1000], "max_actions": 20, "interactive_action_batch_size": 8,
        }})
        store.emit("demo.state", 0, {"state": "running"})
        now = datetime.now(timezone.utc).isoformat()
        clients = tuple(ClientObservation(
            sta_mac=f"02:00:00:00:{number:02x}:00", connected_device_id="02:00:00:00:01:20",
            connected_device_name="Source", connected_bssid="02:00:00:00:01:01",
            rcpi=70, association_uptime_seconds=90, metric_observed_at=now,
            measurement_source="associated_sta_link_metrics", band="5", ssid="private_ssid", cohort="private",
        ) for number in range(3, 12))
        candidates = tuple(CandidateObservation(
            sta_mac=client.sta_mac, bssid="02:00:00:00:04:01", device_id="02:00:00:00:02:20",
            device_name="Target", rcpi=100, metric_observed_at=now, measurement_source="candidate", band="5",
        ) for client in clients)
        snapshot = Snapshot(schema_version=1, sequence=0, controller_url="http://controller", observed_at=now,
                            health=MeshHealth(5, 9), clients=clients, candidates=candidates)
        for number, client in enumerate(clients):
            conductor._role_by_mac[client.sta_mac] = f"sta_static_{number:02}"
            conductor._container_by_mac[client.sta_mac] = f"client-{number}"
        conductor._mac_by_role = {role: mac for mac, role in conductor._role_by_mac.items()}
        room = {"environment_epoch": 1, "revision": 1, "stable_for_seconds": 1,
                "daemon": {"instance_id": "test"},
                "expected_online_clients": len(clients),
                "roles": {conductor._role_by_mac[client.sta_mac]: {"present": True} for client in clients}}
        conductor.room_state = lambda: room
        policy = ThresholdPolicy(_interactive_policy(PolicyConfig(expected_clients=9)))
        provider = Mock(last_raw=[], last_selected_sta_macs={client.sta_mac for client in clients},
                        last_requested_sta_macs=set(), last_selection={}, last_unavailable=None)
        observer = Mock()
        observer.observe.return_value = snapshot
        actuator = Mock()
        actuator.execute.return_value.success = True
        actuator.execute.return_value.to_dict.return_value = {"success": True}
        futures = [Future() for _index in range(6)]
        waits = []

        def advance(*_arguments):
            waits.append(actuator.execute.call_count)
            if len(waits) == 2:
                decision = actuator.execute.call_args_list[0].args[0]
                futures[0].set_result((decision, Mock(success=True), datetime.now(timezone.utc), None))
            return len(waits) == 3

        with patch("room_demo.conductor.load_policy", return_value=policy.config), \
             patch("room_demo.conductor._simulated_bss_channels", return_value={}), \
             patch("room_demo.conductor.ThresholdPolicy", return_value=policy), \
             patch("room_demo.conductor.ControllerCandidateProvider", return_value=provider), \
             patch("room_demo.conductor.StreamingCandidateProvider", return_value=provider), \
             patch("room_demo.conductor.ControllerObserver", return_value=observer), \
             patch("room_demo.conductor.NativeSteerActuator", return_value=actuator), \
             patch.object(policy, "evaluate", wraps=policy.evaluate) as evaluate, \
             patch.object(conductor._verification_executor, "submit", side_effect=futures), \
             patch.object(conductor, "_optimizer_wait", side_effect=advance):
            conductor._optimizer_worker()
        self.assertEqual(conductor.errors, [])
        self.assertEqual(waits, [5, 5, 6])
        queued_state = evaluate.call_args_list[2].args[1]
        for client in clients[5:]:
            pending = queued_state.for_sta(client.sta_mac)
            self.assertEqual(pending.phase, "holding")
            self.assertIsNone(pending.pending_since)
            self.assertIsNone(pending.last_action_at)
            self.assertEqual(pending.failure_count, 0)
        self.assertEqual(actuator.execute.call_args_list[5].args[0].sta_mac, clients[5].sta_mac)

    def test_cooldown_and_failure_backoff_start_at_actual_verification(self):
        now = datetime.now(timezone.utc)
        config = _interactive_policy(PolicyConfig())
        decision = Decision(sta_mac="02:00:00:00:03:00", action="steer", reason="ready",
                            source_bssid="02:00:00:00:01:01", target_bssid="02:00:00:00:02:01")
        for success in (True, False):
            result = _completed_action_state(PolicyState(), decision, config, success, "failed", now).for_sta(decision.sta_mac)
            self.assertEqual(result.phase, "cooldown" if success else "backoff")
            self.assertIsNone(result.pending_since)
            self.assertEqual(result.cooldown_until if success else result.backoff_until, (now + timedelta(seconds=5)).isoformat())

    def test_fast_policy_removes_pre_action_timers_but_keeps_hysteresis_and_bounds(self):
        original = PolicyConfig()
        fast = _interactive_policy(original)
        self.assertEqual((fast.condition_hold_seconds, fast.minimum_dwell_seconds), (0, 0))
        self.assertEqual(fast.minimum_target_gain_rcpi, 4)
        self.assertEqual(fast.post_steer_cooldown_seconds, 5)
        self.assertEqual(fast.steer_timeout_seconds, 40)
        self.assertEqual((fast.failure_backoff_seconds, fast.maximum_failure_backoff_seconds), (5, 30))
        self.assertEqual(fast.reject_stale_metrics_after_seconds, original.reject_stale_metrics_after_seconds)
        self.assertEqual(original.minimum_dwell_seconds, 20)

    def test_both_simultaneously_moved_clients_get_collection_priority(self):
        roles = {"first": "02:00:00:00:03:00", "second": "02:00:00:00:04:00"}
        for mac in roles.values():
            client = Mock(sta_mac=mac, connected_bssid="02:00:00:00:01:01")
            self.assertTrue(_priority_client(client, list(roles), roles, {}, 120, 30))
            self.assertFalse(_priority_client(client, list(roles), roles, {}, 120, 120))

    def test_rf_change_interrupts_optimizer_retry_wait(self):
        conductor, store = self._conductor()
        conductor.interactive = True
        import threading
        import time
        timer = threading.Timer(0.02, lambda: store.emit(
            "optimizer.environment.changed", 0, {"environment_epoch": 1}))
        timer.start()
        started = time.monotonic()
        self.assertFalse(conductor._optimizer_wait(1, 0))
        timer.join()
        self.assertLess(time.monotonic() - started, 0.5)

    def test_superseded_collection_is_not_a_native_outage_or_fatal_error(self):
        conductor, store, policy, actuator, sleeper = self._run_optimizer([CandidateSnapshotSuperseded("RF changed"), None])
        self.assertEqual(conductor.errors, [])
        self.assertEqual(conductor.warnings, [])
        self.assertNotIn("optimizer.measurement.unavailable", store.current()["latest"])
        actuator.execute.assert_not_called()
        self.assertEqual(policy.evaluate.call_count, 1)
        self.assertEqual(sleeper.call_args_list[0].args[0], 0.1)

    def test_collection_observes_cooldown_and_backoff_without_duplicate_pending_work(self):
        client = Mock(sta_mac="02:00:00:00:03:00")
        policy = Mock()
        policy.requires_candidate_measurement.return_value = True
        now = "2026-09-08T00:00:00Z"
        future = "2026-09-08T00:00:30Z"
        for phase, until in (("pending", {}), ("cooldown", {"cooldown_until": future}),
                             ("backoff", {"backoff_until": future})):
            state = PolicyState((ClientPolicyState(sta_mac=client.sta_mac, phase=phase, **until),))
            self.assertEqual(_candidate_measurement_needed(policy, state, client, now), phase != "pending")
            if until:
                self.assertTrue(_candidate_measurement_needed(policy, state, client, future))
        for phase in ("holding", "stable"):
            state = PolicyState((ClientPolicyState(sta_mac=client.sta_mac, phase=phase),))
            self.assertTrue(_candidate_measurement_needed(policy, state, client, now))
        policy.requires_candidate_measurement.return_value = False
        self.assertFalse(_candidate_measurement_needed(policy, PolicyState(), client, now))

    def test_collection_priority_uses_actual_source_and_expires(self):
        client = Mock(sta_mac="02:00:00:00:03:00", connected_bssid="02:00:00:00:01:01")
        clients = {"station": client.sta_mac}
        owners = {client.connected_bssid: "extender_1"}
        self.assertTrue(_priority_client(client, "extender_1", clients, owners, 60, 0))
        self.assertTrue(_priority_client(client, "station", clients, owners, 60, 0))
        self.assertFalse(_priority_client(client, "extender_1", clients, owners, 60, 60))
        self.assertFalse(_priority_client(client, None, clients, owners, 60, 0))
        self.assertFalse(_priority_client(client, "gateway", clients, owners, 60, 0))
        self.assertTrue(_priority_client(client, "extender_1", clients, owners,
                                         CANDIDATE_PRIORITY_WINDOW_SECONDS, 119))
        self.assertFalse(_priority_client(client, "extender_1", clients, owners,
                                          CANDIDATE_PRIORITY_WINDOW_SECONDS, 120))
        client.connected_bssid = "02:00:00:00:02:01"
        self.assertFalse(_priority_client(client, "extender_1", clients, owners, 60, 0))

    def test_eight_client_batch_still_verifies_each_action_and_stops_on_failure(self):
        for failed_verification in (False, True):
            with self.subTest(failed_verification=failed_verification):
                conductor, store = self._conductor()
                conductor.interactive = True
                conductor.mode = "act"
                conductor.manifest.update({"policy": "policy.yaml", "optimizer": {
                    "allow_simulated_candidates": True, "request_only": True, "interval_seconds": 5,
                    "action_window_ms": [0, 1000], "max_actions": 8, "interactive_action_batch_size": 8,
                }})
                store.emit("demo.state", 0, {"state": "running"})
                now = datetime.now(timezone.utc).isoformat()
                clients = tuple(ClientObservation(
                    sta_mac=f"02:00:00:00:{number:02x}:00", connected_device_id="02:00:00:00:01:20",
                    connected_device_name="Source", connected_bssid="02:00:00:00:01:01",
                    rcpi=70, association_uptime_seconds=90, metric_observed_at=now,
                    measurement_source="associated_sta_link_metrics", band="5", ssid="private_ssid", cohort="private",
                ) for number in range(3, 12))
                candidates = tuple(CandidateObservation(
                    sta_mac=client.sta_mac, bssid="02:00:00:00:04:01", device_id="02:00:00:00:02:20",
                    device_name="Target", rcpi=100, metric_observed_at=now, measurement_source="candidate", band="5",
                ) for client in clients)
                snapshot = Snapshot(schema_version=1, sequence=0, controller_url="http://controller", observed_at=now,
                                    health=MeshHealth(5, 9), clients=clients, candidates=candidates)
                for number, client in enumerate(clients):
                    conductor._role_by_mac[client.sta_mac] = f"sta_static_{number:02}"
                    conductor._container_by_mac[client.sta_mac] = f"client-{number}"
                decisions = tuple(Decision(sta_mac=client.sta_mac, action="steer", reason="ready",
                                          source_bssid=client.connected_bssid, target_bssid=candidates[0].bssid,
                                          current_rcpi=70, target_rcpi=100) for client in clients)
                policy = Mock(config=PolicyConfig())
                policy.evaluate.return_value = Evaluation("hash", decisions, PolicyState())
                provider = Mock(last_raw=[], last_selected_sta_macs={client.sta_mac for client in clients}, last_selection={})
                observer = Mock()
                observer.observe.return_value = snapshot
                actuator = Mock()
                actuator.execute.return_value.success = True
                actuator.execute.return_value.to_dict.return_value = {"success": True}
                verifier = Mock()
                verifier.verify.return_value.success = not failed_verification
                verifier.verify.return_value.reason = "association_timeout" if failed_verification else "association_and_traffic_converged"
                verifier.verify.return_value.to_dict.return_value = {"success": not failed_verification}
                with patch("room_demo.conductor.load_policy", return_value=PolicyConfig()), \
                     patch("room_demo.conductor._simulated_bss_channels", return_value={}), \
                     patch("room_demo.conductor.ThresholdPolicy", return_value=policy), \
                     patch("room_demo.conductor.ControllerCandidateProvider", return_value=provider), \
                     patch("room_demo.conductor.ControllerObserver", side_effect=[observer, Mock()]), \
                     patch("room_demo.conductor.SteerActuator", return_value=actuator), \
                     patch("room_demo.conductor.OutcomeVerifier", return_value=verifier), \
                     patch.object(conductor, "_sleep", return_value=True):
                    conductor._run_worker("optimizer", conductor._optimizer_worker)
                self.assertEqual(conductor.errors, [])
                self.assertEqual(actuator.execute.call_count, 1 if failed_verification else 8)
                self.assertEqual(verifier.verify.call_count, actuator.execute.call_count)
                if failed_verification:
                    self.assertEqual(store.current()["latest"]["optimizer.batch.aborted"]["payload"]["reason"], "verification_failed")

    def test_long_batch_rechecks_original_serving_and_target_timestamps(self):
        now = datetime.now(timezone.utc)
        timestamp = now.isoformat()
        client = ClientObservation(
            sta_mac="02:00:00:00:03:00", connected_device_id="02:00:00:00:01:20",
            connected_device_name="Source", connected_bssid="02:00:00:00:01:01",
            rcpi=70, association_uptime_seconds=90, metric_observed_at=timestamp,
            measurement_source="associated_sta_link_metrics", band="5", ssid="private_ssid", cohort="private",
        )
        candidate = CandidateObservation(
            sta_mac=client.sta_mac, bssid="02:00:00:00:02:01", device_id="02:00:00:00:02:20",
            device_name="Target", rcpi=100, metric_observed_at=timestamp,
            measurement_source="candidate", band="5",
        )
        snapshot = Snapshot(schema_version=1, sequence=0, controller_url="http://controller",
                            observed_at=timestamp, health=MeshHealth(5, 1), clients=(client,), candidates=(candidate,))
        decision = Decision(sta_mac=client.sta_mac, action="steer", reason="ready",
                            source_bssid=client.connected_bssid, target_bssid=candidate.bssid)
        self.assertTrue(_action_measurements_fresh(decision, snapshot, 60, now + timedelta(seconds=59)))
        self.assertFalse(_action_measurements_fresh(decision, snapshot, 60, now + timedelta(seconds=61)))
        self.assertFalse(_action_measurements_fresh(decision, replace(snapshot, candidates=()), 60, now))
        self.assertFalse(_action_measurements_fresh(decision, replace(snapshot, clients=(replace(client, metric_observed_at=None),)), 60, now))
        self.assertFalse(_action_measurements_fresh(decision, snapshot, 60, now - timedelta(seconds=1)))

    def test_network_updates_continue_during_optimizer_measurement(self):
        conductor, store = self._conductor()
        conductor._candidate_active.set()
        conductor._controller_lock.acquire()
        observer = Mock(last_raw={"topology": {"nodes": []}})
        with patch("room_demo.conductor.ControllerObserver", return_value=observer), \
             patch.object(conductor, "_wait_for_run", return_value=True), \
             patch.object(conductor, "_active", return_value=True), \
             patch.object(conductor, "_sleep", return_value=True), \
             patch.object(conductor, "_network_payload", return_value={"clients": []}):
            try:
                conductor._network_worker()
            finally:
                conductor._controller_lock.release()
        observer.observe.assert_called_once()
        self.assertIn("network.snapshot", store.current()["latest"])

    def test_passive_network_observer_does_not_capture_optimizer_local_variables(self):
        conductor, store = self._conductor()
        conductor.interactive = True
        conductor.room_state = lambda: {"environment_epoch": 1}
        observer = Mock(last_raw={"topology": {"nodes": []}})
        observer.observe_topology.return_value = Snapshot(
            schema_version=1, sequence=0, observed_at="2026-09-08T00:00:00Z",
            controller_url="http://controller", health=MeshHealth(0, 0), clients=(), candidates=())
        def construct(*arguments, **options):
            self.assertNotIn('current_metric_floor', options)
            return observer
        with patch('room_demo.conductor.ControllerObserver', side_effect=construct), \
             patch.object(conductor, '_wait_for_run', return_value=True), \
             patch.object(conductor, '_active', return_value=True), \
             patch.object(conductor, '_sleep', return_value=True), \
             patch.object(conductor, '_network_payload', return_value={'clients': []}):
            conductor._network_worker()
        self.assertIn('network.snapshot', store.current()['latest'])
        observer.observe.assert_not_called()
        observer.observe_topology.assert_called_once()

    def test_slow_client_probe_does_not_block_a_different_client(self):
        conductor, _store = self._conductor()
        started = threading.Event()
        release = threading.Event()
        room = {"roles": {role: {"present": True} for role in conductor._mac_by_role}}
        clients = [Mock(sta_mac=mac, connected_bssid="02:00:00:00:04:01", band="5")
                   for mac in conductor._mac_by_role.values()]
        def probe(container, *_arguments):
            if container == conductor.hero_container:
                started.set()
                self.assertTrue(release.wait(3))
            return None
        with ThreadPoolExecutor(max_workers=2) as executor, patch("room_demo.conductor.read_client_link", side_effect=probe):
            first = executor.submit(conductor._client_link_fallback, clients[0], room)
            try:
                self.assertTrue(started.wait(1))
                second = executor.submit(conductor._client_link_fallback, clients[1], room)
                self.assertIs(second.result(timeout=1), clients[1])
            finally:
                release.set()
            self.assertIs(first.result(timeout=1), clients[0])

    def test_display_metrics_never_replace_native_roster_or_association(self):
        conductor, _store = self._conductor()
        client = ClientObservation(sta_mac=conductor.hero_mac, connected_device_id="02:00:00:00:01:20",
            connected_device_name="Extender", connected_bssid="02:00:00:00:04:01", band="5",
            rcpi=None, metric_observed_at=None, association_uptime_seconds=0, measurement_source="topology")
        snapshot = Snapshot(schema_version=1, sequence=0, observed_at="2026-09-08T00:00:10Z",
            controller_url="http://controller", health=MeshHealth(1, 1), clients=(client,), candidates=())
        sample = replace(client, rcpi=120, metric_observed_at="2026-09-08T00:00:09Z")
        conductor._network_metrics = {client.sta_mac: sample}
        room = {"last_rf_applied_at": "2026-09-08T00:00:08Z"}
        self.assertEqual(conductor._merge_network_metrics(snapshot, room).clients[0].rcpi, 120)
        room["last_rf_applied_at"] = "2026-09-08T00:00:10Z"
        self.assertIsNone(conductor._merge_network_metrics(snapshot, room).clients[0].rcpi)
        conductor._network_metrics = {client.sta_mac: replace(sample, connected_bssid="02:00:00:00:01:01")}
        self.assertEqual(conductor._merge_network_metrics(snapshot, None).clients, (client,))
        conductor._network_metrics = {}
        self.assertEqual(conductor._merge_network_metrics(snapshot, None).clients, (client,))

    def test_busy_rf_transaction_does_not_block_display_projection(self):
        conductor, _store = self._conductor()
        conductor.room_projection = lambda: None
        conductor.room_state = Mock(side_effect=AssertionError("blocking RF read"))
        self.assertTrue(conductor._projected_room()["projection_busy"])
        conductor.room_state.assert_not_called()

    def test_metric_worker_publishes_each_client_without_a_batch_barrier(self):
        conductor, _store = self._conductor()
        client = Mock(sta_mac=conductor.hero_mac)
        conductor._network_clients = (client,)
        conductor.room_state = lambda: {"environment_epoch": 1}
        def construct(*arguments, **options):
            def measure(clients):
                options["current_link_progress"](clients[0])
                self.assertIs(conductor._network_metrics[client.sta_mac], client)
            self.assertEqual(options["max_current_metric_age_seconds"], 5)
            return Mock(metrics_for=measure)
        with patch("room_demo.conductor.ControllerObserver", side_effect=construct), \
             patch.object(conductor, "_wait_for_run", return_value=True), \
             patch.object(conductor, "_active", return_value=True), \
             patch.object(conductor, "_sleep", return_value=True):
            conductor._network_metrics_worker()

    def test_kernel_link_fallback_is_cached_and_invalidated_by_rf_epoch(self):
        conductor, _store = self._conductor()
        client = Mock(sta_mac=conductor.hero_mac, connected_bssid="02:00:00:00:04:01", band="5")
        room = {"roles": {"sta_mobile_01": {"present": True}}, "environment_epoch": 1, "measurement_epoch": 1}
        with patch("room_demo.conductor.read_client_link", return_value=None) as read_link:
            self.assertIs(conductor._client_link_fallback(client, room), client)
            conductor._client_link_fallback(client, room)
            read_link.assert_called_once()
            room["environment_epoch"] = 2
            conductor._client_link_fallback(client, room)
            self.assertEqual(read_link.call_count, 2)
            room["roles"]["sta_mobile_01"]["present"] = False
            conductor._client_link_fallback(client, room)
            self.assertEqual(read_link.call_count, 2)

    def test_traffic_probe_switches_container_without_changing_optimizer_hero(self):
        conductor, store = self._conductor()
        conductor.manifest["traffic"]["interval_seconds"] = 2
        room = {"traffic_probe": {"role": "sta_static_01", "selection": 1},
                "roles": {"sta_static_01": {"present": True}}}
        conductor.room_state = lambda: room
        with patch.object(conductor, "_wait_for_run", return_value=True), \
             patch.object(conductor, "_active", return_value=True), \
             patch.object(conductor, "_sleep", return_value=True), \
             patch.object(conductor, "_ping", return_value={"success": True, "rtt_ms": 1}) as ping:
            conductor._traffic_worker()
            ping.assert_called_once_with("wlan-client")
            self.assertEqual(store.current()["latest"]["traffic.sample"]["payload"]["traffic_probe"]["role"], "sta_static_01")
            self.assertEqual(conductor.hero_container, "wlan-client-007")
            room["roles"]["sta_static_01"]["present"] = False
            ping.reset_mock()
            conductor._traffic_worker()
            ping.assert_not_called()
            self.assertEqual(store.current()["latest"]["traffic.sample"]["payload"]["status"], "offline")

    def test_inflight_ping_is_not_relabelled_after_probe_selection_changes(self):
        conductor, store = self._conductor()
        conductor.manifest["traffic"]["interval_seconds"] = 2
        room = {"traffic_probe": {"role": "sta_mobile_01", "selection": 0}}
        conductor.room_state = lambda: room
        def switched(_container):
            room["traffic_probe"] = {"role": "sta_static_01", "selection": 1}
            return {"success": True}
        with patch.object(conductor, "_wait_for_run", return_value=True), \
             patch.object(conductor, "_active", return_value=True), \
             patch.object(conductor, "_sleep", return_value=True), \
             patch.object(conductor, "_ping", side_effect=switched):
            conductor._traffic_worker()
        self.assertNotIn("traffic.sample", store.current()["latest"])

    def _run_optimizer(self, observations, *, interactive=True, evaluation_state=None, profiling=False):
        conductor, store = self._conductor()
        conductor.interactive = interactive
        conductor.profiling = profiling
        conductor.mode = "act"
        conductor.manifest.update({"policy": "policy.yaml", "optimizer": {
            "allow_simulated_candidates": True, "request_only": True,
            "interval_seconds": 5, "action_window_ms": [0, 1000], "max_actions": 10,
        }})
        store.emit("demo.state", 0, {"state": "running"})
        snapshot = Snapshot(
            schema_version=1, sequence=1, controller_url="http://controller",
            observed_at="2026-09-03T00:00:00Z", health=MeshHealth(1, 1),
            clients=(), candidates=(),
        )
        observer = Mock()
        observer.observe.side_effect = [snapshot if item is None else item for item in observations]
        policy = Mock(config=PolicyConfig())
        policy.evaluate.return_value = Evaluation("hash", (Decision(
            sta_mac=conductor.hero_mac, action="none", reason="test_observed",
            source_bssid="02:00:00:00:01:01",
        ),), evaluation_state or PolicyState())
        provider = Mock(last_raw=[{"request": {}, "error": "HTTP 504"}], last_selected_sta_macs=set(), last_selection={})
        actuator = Mock()
        with patch("room_demo.conductor.load_policy", return_value=PolicyConfig()), \
             patch("room_demo.conductor._simulated_bss_channels", return_value={}), \
             patch("room_demo.conductor.ThresholdPolicy", return_value=policy), \
             patch("room_demo.conductor.ControllerCandidateProvider", return_value=provider) as candidate_factory, \
             patch("room_demo.conductor.ControllerObserver", side_effect=[observer, Mock()]), \
             patch("room_demo.conductor.SteerActuator", return_value=actuator), \
             patch.object(conductor, "_optimizer_wait" if profiling else "_sleep",
                          side_effect=[False] * (len(observations) - 1) + [True]) as sleeper:
            conductor._run_worker("optimizer", conductor._optimizer_worker)
        self.assertEqual(candidate_factory.call_args.kwargs["busy_wait_seconds"], 1 if interactive else 0)
        self.assertFalse(conductor._candidate_active.is_set())
        self.assertFalse(conductor._controller_lock.locked())
        return conductor, store, policy, actuator, sleeper

    def test_profile_preserves_policy_until_world_identity_changes(self):
        holding = PolicyState((ClientPolicyState(
            sta_mac="02:00:00:00:0c:00", phase="holding",
            condition_since="2026-09-03T00:00:00Z",
        ),))
        with patch.object(EventStore, "world_epoch", side_effect=[1, 1, 1, 2, 2]):
            conductor, _store, policy, _actuator, _sleeper = self._run_optimizer(
                [None, None, None], evaluation_state=holding, profiling=True,
            )
        self.assertEqual(conductor.errors, [])
        self.assertEqual([call.args[1] for call in policy.evaluate.call_args_list],
                         [PolicyState(), holding, PolicyState()])

    def test_interactive_measurement_outage_retries_without_steering(self):
        conductor, store, policy, actuator, sleeper = self._run_optimizer([
            CandidateMetricsUnavailable("HTTP 504"), CandidateMetricsUnavailable("HTTP 504"), None,
        ])
        self.assertEqual(conductor.errors, [])
        self.assertEqual(len(conductor.warnings), 2)
        self.assertEqual(store.current()["state"], "running")
        self.assertEqual(policy.evaluate.call_count, 1)
        self.assertEqual(conductor.action_attempts, 0)
        actuator.execute.assert_not_called()
        self.assertEqual([call.args[0] for call in sleeper.call_args_list[:2]], [1, 2])
        unavailable = store.current()["latest"]["optimizer.measurement.unavailable"]["payload"]
        self.assertEqual(unavailable["consecutive_failures"], 2)
        self.assertFalse(unavailable["automatic_actuation_ready"])
        self.assertFalse(unavailable["fleet"]["converged"])
        self.assertEqual(len(unavailable["failed_transactions"]), 1)
        self.assertEqual(store.current()["optimizer"]["decision"]["reason"], "test_observed")
        self.assertNotIn("status", store.current()["optimizer"])

    def test_outage_backoff_is_capped_and_resets_after_a_good_measurement(self):
        failures = [CandidateMetricsUnavailable("HTTP 504") for _index in range(5)]
        conductor, store, _policy, actuator, sleeper = self._run_optimizer(
            failures + [None, CandidateMetricsUnavailable("HTTP 504")]
        )
        waits = [call.args[0] for call in sleeper.call_args_list]
        self.assertEqual(waits[:5], [1, 2, 4, 8, 8])
        self.assertEqual(waits[-1], 1)
        self.assertEqual(conductor.errors, [])
        self.assertEqual(store.current()["optimizer"]["status"], "unavailable")
        actuator.execute.assert_not_called()

    def test_controller_timeout_pauses_interactive_steering_without_resetting_room(self):
        conductor, store, policy, actuator, sleeper = self._run_optimizer([
            TimeoutError("controller read timed out"), None,
        ])
        self.assertEqual(conductor.errors, [])
        self.assertEqual(len(conductor.warnings), 1)
        self.assertEqual(policy.evaluate.call_count, 1)
        actuator.execute.assert_not_called()
        self.assertEqual(sleeper.call_args_list[0].args[0], 1)
        unavailable = store.current()["latest"]["optimizer.measurement.unavailable"]["payload"]
        self.assertEqual(unavailable["reason"], "controller_transport_unavailable")
        self.assertFalse(unavailable["automatic_actuation_ready"])

    def test_outage_restarts_an_unacted_hold_before_the_next_evaluation(self):
        holding = PolicyState((ClientPolicyState(
            sta_mac="02:00:00:00:0c:00", phase="holding",
            condition_since="2026-09-03T00:00:00Z", last_action_at="2026-09-02T00:00:00Z",
        ),))
        conductor, _store, policy, _actuator, _sleeper = self._run_optimizer(
            [None, CandidateMetricsUnavailable("HTTP 504"), None], evaluation_state=holding,
        )
        prior = policy.evaluate.call_args_list[-1].args[1].for_sta(conductor.hero_mac)
        self.assertEqual(prior.phase, "stable")
        self.assertIsNone(prior.condition_since)
        self.assertEqual(prior.last_action_at, "2026-09-02T00:00:00Z")

    def test_outage_keeps_pending_cooldown_and_backoff_history(self):
        for phase in ["pending", "cooldown", "backoff"]:
            prior = PolicyState((ClientPolicyState(
                sta_mac="02:00:00:00:0c:00", phase=phase,
                target_bssid="02:00:00:00:04:01", failure_count=2,
                pending_since="2026-09-03T00:00:00Z", cooldown_until="2026-09-03T00:00:30Z",
                backoff_until="2026-09-03T00:01:00Z", last_action_at="2026-09-03T00:00:00Z",
            ),))
            self.assertEqual(_interrupted_measurement_state(prior), prior)

    def test_scripted_measurement_outage_still_fails_closed(self):
        conductor, _store, policy, actuator, sleeper = self._run_optimizer(
            [CandidateMetricsUnavailable("HTTP 504")], interactive=False,
        )
        self.assertEqual(len(conductor.errors), 1)
        policy.evaluate.assert_not_called()
        actuator.execute.assert_not_called()
        sleeper.assert_not_called()

    def test_invalid_candidate_measurement_still_fails_closed_in_interactive_mode(self):
        conductor, _store, policy, actuator, sleeper = self._run_optimizer(
            [CandidateMetricsError("unexpected measurement")],
        )
        self.assertEqual(len(conductor.errors), 1)
        policy.evaluate.assert_not_called()
        actuator.execute.assert_not_called()
        sleeper.assert_not_called()

    def test_live_simulated_channels_are_mapped_per_bssid(self):
        plan = {"bindings": {
            "extender_1": {
                "role_type": "fronthaul_ap",
                "band_radios": {"6": {
                    "frequency_mhz": 5955,
                    "interfaces": [{
                        "mac": "02:00:00:00:01:60",
                        "frequency_mhz": 5955,
                    }],
                }},
            },
            "extender_2": {
                "role_type": "fronthaul_ap",
                "band_radios": {"6": {
                    "frequency_mhz": 6135,
                    "interfaces": [{
                        "mac": "02:00:00:00:02:60",
                        "frequency_mhz": 6135,
                    }],
                }},
            },
        }}

        self.assertEqual(_simulated_bss_channels(plan), {
            "02:00:00:00:01:60": 1,
            "02:00:00:00:02:60": 37,
        })

    def test_unexpected_worker_failure_is_retained_as_fatal(self):
        conductor, store = self._conductor()

        def broken():
            raise AttributeError("synthetic worker fault")

        conductor._run_worker("optimizer", broken)
        self.assertEqual(len(conductor.errors), 1)
        self.assertIn("synthetic worker fault", conductor.errors[0])
        event = store.current()["latest"]["worker.error"]
        self.assertTrue(event["payload"]["fatal"])

    def _conductor(self):
        world = {"name": "world", "duration_ms": 1000, "tick_ms": 100}
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = EventStore("run", world, Path(temp.name) / "live-events.jsonl")
        plan = {
            "bindings": {
                "sta_mobile_01": {
                    "role_type": "station",
                    "radio_permanent_mac": "02:00:00:00:0c:00",
                    "container": "wlan-client-007",
                },
                "sta_static_01": {
                    "role_type": "station",
                    "radio_permanent_mac": "02:00:00:00:03:00",
                    "container": "wlan-client",
                },
                "extender_1": {
                    "role_type": "fronthaul_ap",
                    "container": "bpiap",
                    "radio_permanent_mac": "02:00:00:00:01:00",
                    "band_radios": {
                        "5": {"interfaces": [{
                            "mac": "02:00:00:00:04:01",
                            "ssid": "private_ssid",
                        }]}
                    },
                },
                "gateway": {
                    "role_type": "fronthaul_ap",
                    "container": "bpibroadband",
                    "radio_permanent_mac": "02:00:00:00:00:00",
                    "band_radios": {
                        "5": {"interfaces": [{
                            "mac": "02:00:00:00:01:01",
                            "ssid": "private_ssid",
                        }]}
                    },
                }
            }
        }
        manifest = {
            "hero": {"role": "sta_mobile_01"},
            "traffic": {"target": "10.0.0.1", "timeout_seconds": 1},
        }
        return LiveConductor(
            store, plan, manifest, mode="recommend", repo_root=Path(temp.name)
        ), store

    def test_closed_action_window_preserves_holding_without_false_pending(self):
        sta = "02:00:00:00:0c:00"
        pending = ClientPolicyState(
            sta_mac=sta,
            phase="pending",
            source_bssid="02:00:00:00:01:01",
            target_bssid="02:00:00:00:04:01",
            condition_since="2026-09-03T00:00:00Z",
            pending_since="2026-09-03T00:00:10Z",
            last_action_at="2026-09-03T00:00:10Z",
        )
        evaluation = Evaluation(
            "hash",
            (Decision(
                sta_mac=sta,
                action="steer",
                reason="threshold_margin_hold_satisfied",
                source_bssid="02:00:00:00:01:01",
                target_bssid="02:00:00:00:04:01",
            ),),
            PolicyState((pending,)),
        )
        state = _deferred_state(PolicyState(), evaluation).for_sta(sta)
        self.assertEqual(state.phase, "holding")
        self.assertIsNone(state.pending_since)
        self.assertIsNone(state.last_action_at)

    def test_unsubmitted_fleet_actions_remain_eligible(self):
        first = "02:00:00:00:03:00"
        second = "02:00:00:00:0c:00"
        pending = tuple(
            ClientPolicyState(
                sta_mac=sta,
                phase="pending",
                source_bssid="02:00:00:00:01:01",
                target_bssid="02:00:00:00:04:01",
                condition_since="2026-09-03T00:00:00Z",
                pending_since="2026-09-03T00:00:10Z",
                last_action_at="2026-09-03T00:00:10Z",
            )
            for sta in (first, second)
        )
        evaluation = Evaluation(
            "hash",
            tuple(Decision(
                sta_mac=sta,
                action="steer",
                reason="threshold_margin_hold_satisfied",
                source_bssid="02:00:00:00:01:01",
                target_bssid="02:00:00:00:04:01",
            ) for sta in (first, second)),
            PolicyState(pending),
        )

        state = _deferred_state(PolicyState(), evaluation)

        self.assertEqual(state.for_sta(first).phase, "holding")
        self.assertIsNone(state.for_sta(first).pending_since)
        self.assertEqual(state.for_sta(second).phase, "holding")
        self.assertIsNone(state.for_sta(second).pending_since)

    def test_interactive_batch_prioritizes_weak_links_and_honors_limit(self):
        decisions = (
            Decision(
                sta_mac="02:00:00:00:03:00", action="steer", reason="ready",
                source_bssid="02:00:00:00:01:01",
                target_bssid="02:00:00:00:04:01",
                current_rcpi=110, target_rcpi=150,
            ),
            Decision(
                sta_mac="02:00:00:00:04:00", action="steer", reason="ready",
                source_bssid="02:00:00:00:01:01",
                target_bssid="02:00:00:00:04:01",
                current_rcpi=80, target_rcpi=100,
            ),
            Decision(
                sta_mac="02:00:00:00:05:00", action="steer", reason="ready",
                source_bssid="02:00:00:00:01:01",
                target_bssid="02:00:00:00:04:01",
                current_rcpi=80, target_rcpi=130,
            ),
            Decision(
                sta_mac="02:00:00:00:06:00", action="hold", reason="stable",
                source_bssid="02:00:00:00:01:01", current_rcpi=60,
            ),
        )

        batch = _ranked_action_batch(decisions, 2)

        self.assertEqual(
            [item.sta_mac for item in batch],
            ["02:00:00:00:05:00", "02:00:00:00:04:00"],
        )

    def test_fleet_convergence_uses_measured_best_ap_not_hold_phase(self):
        clients = (
            ClientObservation(
                sta_mac="02:00:00:00:03:00",
                connected_device_id="02:00:00:00:00:01",
                connected_device_name="Agent-1",
                connected_bssid="02:00:00:00:01:01",
                rcpi=80,
                association_uptime_seconds=90,
                metric_observed_at="2026-09-03T00:00:00Z",
                measurement_source="associated_sta_link_metrics",
                band="5", ssid="private_ssid", cohort="private",
            ),
        )
        from optimizer.model import CandidateObservation
        snapshot = Snapshot(
            schema_version=1, sequence=0,
            observed_at="2026-09-03T00:00:00Z",
            controller_url="http://controller",
            health=MeshHealth(devices=5, clients=1, bsses=50),
            clients=clients,
            candidates=(CandidateObservation(
                sta_mac=clients[0].sta_mac,
                bssid="02:00:00:00:04:01",
                device_id="02:00:00:00:00:04",
                device_name="Extender-1",
                rcpi=138,
                metric_observed_at="2026-09-03T00:00:00Z",
                measurement_source="candidate",
                band="5",
            ),),
        )

        status = _fleet_status(snapshot, {clients[0].sta_mac})

        self.assertFalse(status["converged"])
        self.assertEqual(status["clients_with_stronger_ap"], 1)
        self.assertEqual(status["stronger_candidates"][0]["gain_rcpi"], 58)
        marginal = replace(snapshot, candidates=(replace(snapshot.candidates[0], rcpi=82),))
        fleet = _fleet_status(marginal, {clients[0].sta_mac}, minimum_gain_rcpi=4)
        self.assertTrue(fleet["converged"])
        partial_roster = _fleet_status(marginal, {clients[0].sta_mac}, minimum_gain_rcpi=4,
                                      expected_sta_macs={clients[0].sta_mac, "02:00:00:00:99:00"})
        self.assertFalse(partial_roster["converged"])
        self.assertFalse(partial_roster["measurement_complete"])
        self.assertEqual(partial_roster["missing_clients"], ["02:00:00:00:99:00"])
        offline_native_client = _fleet_status(marginal, {clients[0].sta_mac}, minimum_gain_rcpi=4,
            expected_sta_macs={clients[0].sta_mac},
            native_sta_macs={clients[0].sta_mac, "02:00:00:00:99:00"})
        self.assertEqual(offline_native_client["unexpected_clients"], ["02:00:00:00:99:00"])
        self.assertFalse(offline_native_client["roster_complete"])
        self.assertFalse(offline_native_client["measurement_complete"])
        self.assertFalse(offline_native_client["converged"])
        self.assertFalse(fleet["absolute_best_converged"])
        self.assertEqual(fleet["clients_outside_policy_margin"], 0)
        self.assertFalse(_fleet_status(replace(snapshot, clients=(), candidates=()), set())["converged"])
        stale = replace(snapshot, observed_at="2026-09-03T00:02:00Z")
        status = _fleet_status(stale, {clients[0].sta_mac})
        self.assertFalse(status["converged"])
        self.assertEqual(status["clients_checked"], 0)
        self.assertEqual(status["candidate_measurements"], 0)
        missing = replace(snapshot, candidates=())
        self.assertFalse(_fleet_status(missing, {clients[0].sta_mac})["converged"])
        partial = replace(snapshot, candidates=(
            replace(snapshot.candidates[0], rcpi=70),
            replace(snapshot.candidates[0], bssid="02:00:00:00:05:01", rcpi=None),
        ))
        self.assertEqual(_fleet_status(partial, {clients[0].sta_mac})["clients_checked"], 0)
        other_band = replace(partial, candidates=(partial.candidates[0], replace(partial.candidates[1], band="6")))
        self.assertTrue(_fleet_status(other_band, {clients[0].sta_mac})["converged"])
        decision = Decision(sta_mac=clients[0].sta_mac, action="none", reason="minimum_dwell_not_met",
                            source_bssid=clients[0].connected_bssid, current_rcpi=80)
        immature = replace(snapshot, clients=(replace(clients[0], association_uptime_seconds=0),))
        evaluation = Evaluation("test", (decision,), PolicyState())
        rows = _client_optimizer_status(immature, evaluation, PolicyConfig(), set())
        self.assertEqual(rows[0]["wait_remaining_seconds"], 20)
        self.assertEqual(rows[0]["association_uptime_seconds"], 0)
        self.assertFalse(rows[0]["candidate_query_selected"])

    def test_interactive_action_window_is_not_bound_to_scenario_time(self):
        conductor, _store = self._conductor()
        conductor.interactive = True

        self.assertEqual(
            conductor._action_window(999_999, [150_000, 220_000]),
            (True, "stable_interactive_environment"),
        )

    def test_interactive_subject_is_the_last_present_moved_client(self):
        conductor, _store = self._conductor()
        conductor.interactive = True
        room = {
            "last_rf_role": "sta_static_01",
            "roles": {"sta_static_01": {"present": True}},
        }

        self.assertEqual(
            conductor._optimization_subject(room),
            ("sta_static_01", "02:00:00:00:03:00", "wlan-client"),
        )
        room["roles"]["sta_static_01"]["present"] = False
        self.assertIsNone(conductor._optimization_subject(room))

    def test_controller_observation_is_projected_to_world_role(self):
        conductor, _store = self._conductor()
        snapshot = Snapshot(
            schema_version=1,
            sequence=0,
            observed_at="2026-09-03T00:00:00Z",
            controller_url="http://controller",
            health=MeshHealth(devices=5, clients=1, bsses=50),
            clients=(ClientObservation(
                sta_mac="02:00:00:00:0c:00",
                connected_device_id="02:00:00:00:00:01",
                connected_device_name="Extender-4",
                connected_bssid="02:00:00:00:04:01",
                rcpi=138,
                association_uptime_seconds=90,
                metric_observed_at="2026-09-03T00:00:00Z",
                measurement_source="associated_sta_link_metrics",
                band="5",
                ssid="private_ssid",
                cohort="private",
            ),),
            candidates=(),
        )
        payload = conductor._network_payload(snapshot)
        self.assertEqual(payload["hero"]["role"], "sta_mobile_01")
        self.assertEqual(payload["hero"]["connected_role"], "extender_1")
        self.assertEqual(payload["hero"]["connected_world_name"], "Extender-4")
        self.assertEqual(payload["hero"]["rssi_dbm"], -41)
        self.assertEqual(payload["cohorts"], {"private": 1, "iot": 0, "other": 0})

    def test_controller_backhaul_is_projected_by_bssid_not_display_ordinal(self):
        conductor, _store = self._conductor()
        topology = {
            "nodes": [
                {
                    "id": "02:00:00:00:10:20",
                    "name": "Agent-1",
                    "backhaulMedia": "Ethernet",
                    "haulTypes": [{"BSSList": [{
                        "BSSID": "02:00:00:00:01:01",
                    }]}],
                },
                {
                    "id": "02:00:00:00:44:20",
                    # Deliberately differs from the world role's ordinal.
                    "name": "Extender-4",
                    "backhaulMedia": "Wireless LAN",
                    "upstreamBSSID": "02:00:00:00:02:01",
                    "haulTypes": [{"BSSList": [
                        {
                            # Parent BSSID must not steal the child's role.
                            "BSSID": "02:00:00:00:01:01",
                            "vapMode": 1,
                        },
                        {
                            "BSSID": "02:00:00:00:04:01",
                            "vapMode": 0,
                        },
                    ]}],
                },
            ],
            "edges": [{
                "from": "02:00:00:00:10:20",
                "to": "02:00:00:00:44:20",
                "mediaType": "Wireless LAN",
                "band": 1,
                "channel": 36,
                "upstreamBSSID": "02:00:00:00:02:01",
                "backhaulSTA": "02:00:00:00:03:01",
                "signal": {"status": "fresh", "rcpi": 138, "rssi_dbm": -41},
            }],
        }

        mesh = conductor._topology_payload(topology)

        self.assertTrue(mesh["available"])
        self.assertEqual(mesh["unresolved_edges"], 0)
        self.assertEqual(
            {item["role"]: item["name"] for item in mesh["nodes"]},
            {"gateway": "Agent-1", "extender_1": "Extender-4"},
        )
        self.assertEqual(mesh["backhaul_edges"], [{
            "parent_role": "gateway",
            "child_role": "extender_1",
            "media_type": "Wireless LAN",
            "band": "5",
            "channel": 36,
            "upstream_bssid": "02:00:00:00:02:01",
            "backhaul_sta": "02:00:00:00:03:01",
            "signal": {"status": "fresh", "rcpi": 138, "rssi_dbm": -41},
        }])


if __name__ == "__main__":
    unittest.main()
