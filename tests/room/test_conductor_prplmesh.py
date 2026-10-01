from __future__ import annotations

import pytest

import tempfile
from concurrent.futures import Future
from datetime import datetime, timezone
import unittest
from unittest.mock import Mock, patch
from pathlib import Path
import sys

if sys.version_info < (3, 9):
    raise unittest.SkipTest("optimizer runtime requires Python 3.9 or newer")

from optimizer.model import CandidateObservation, ClientObservation, MeshHealth, Snapshot
from optimizer.candidates import CandidateMetricsUnavailable
from optimizer.policy import Decision, Evaluation, PolicyConfig, ThresholdPolicy
from optimizer.state import ClientPolicyState, PolicyState
from room_service.conductor import (
    LiveConductor, _candidate_measurement_needed, _deferred_state, _interactive_policy,
)
from room_service.events import EventStore


@pytest.fixture(autouse=True)
def _prplmesh_stack(monkeypatch):
    # prplMesh's conductor: its observer, candidates, topology and tuning (room_service.lab)
    monkeypatch.setenv("OPTIMIZER_STACK", "prplmesh")


class ConductorProjectionTests(unittest.TestCase):
    def test_received_scan_role_projection_preserves_evidence_and_does_not_mutate_provider(self):
        from room_service.conductor import _received_scan_status
        neighbor = {"bssid": "ap", "observed_at": "2026-09-15T12:00:00Z",
                    "advertised_bss_load": {"state": "available", "utilization": 0}}
        status = {"station": {"available": True, "source": "client_nl80211_received_scan",
                              "neighbors": [neighbor, {"bssid": "unknown"}]}}
        projected = _received_scan_status(status, {"ap": "gateway"}, {"station": "client"})
        self.assertEqual(projected["station"]["receiver_role"], "client")
        self.assertEqual(projected["station"]["neighbors"][0], {**neighbor, "role": "gateway"})
        self.assertIsNone(projected["station"]["neighbors"][1]["role"])
        self.assertNotIn("receiver_role", status["station"])
        self.assertNotIn("role", neighbor)
        self.assertEqual(_received_scan_status({"station": {"available": False}}, {}, {}),
                         {"station": {"available": False, "receiver_role": None, "neighbors": []}})

    def test_load_aware_optimizer_registers_owner_hook_before_collection(self):
        conductor, _store = self._conductor()
        conductor.interactive = False
        conductor.profiling = False
        conductor.manifest.update({"policy": "policy.yaml", "optimizer": {
            "allow_simulated_candidates": True, "request_only": True, "interval_seconds": 5,
            "action_window_ms": [0, 1000], "max_actions": 1,
        }})
        receiver = Mock()
        with patch("room_service.conductor.load_policy", return_value=PolicyConfig(load_aware_enabled=True)), \
             patch("room_service.conductor.NativeLoadProvider", return_value=receiver), \
             patch("room_service.conductor.PrplMeshCandidateProvider"), \
             patch("room_service.conductor.PrplMeshObserver", side_effect=RuntimeError("observer constructed")) as observer, \
             patch.object(conductor, "_wait_for_run", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "observer constructed"):
                conductor._optimizer_worker()
        self.assertIs(observer.call_args.kwargs.get("ownership_observer"), receiver.observe_owners)

    def test_profile_candidate_epochs_invalidate_only_affected_clients(self):
        conductor, _store = self._conductor()
        conductor._role_by_mac = {"first": "sta_01", "second": "sta_02"}
        before = {"candidate_epochs": {"global": 1, "roles": {"sta_01": 0}}}
        after = {"candidate_epochs": {"global": 1, "roles": {"sta_01": 1}}}
        self.assertEqual(conductor._candidate_changes({"first", "second"}, before, after), {"first"})
        after["candidate_epochs"]["global"] = 2
        self.assertEqual(conductor._candidate_changes({"first", "second"}, before, after), {"first", "second"})
        self.assertEqual(conductor._candidate_changes({"first"}, {"environment_epoch": 1},
                                                      {"environment_epoch": 2}), {"first"})

    def test_unavailable_topology_keeps_observation_workers_alive_without_publishing_old_data(self):
        for worker, method in (("network", "observe_topology"), ("network-metrics", "metrics_for")):
            with self.subTest(worker=worker):
                conductor, store = self._conductor()
                conductor.interactive = True
                conductor._network_clients = (Mock(),)
                observer = Mock()
                operation = getattr(observer, method)
                operation.side_effect = [CandidateMetricsUnavailable("native topology unavailable"), Mock()]
                observer.last_raw = {"topology": {}}
                with patch("room_service.conductor.PrplMeshObserver", return_value=observer), \
                     patch.object(conductor, "_wait_for_run", return_value=True), \
                     patch.object(conductor, "_active", return_value=True), \
                     patch.object(conductor, "_sleep", side_effect=[False, True]), \
                     patch.object(conductor, "_merge_network_metrics"), \
                     patch.object(conductor, "_network_payload", return_value={"clients": []}) as project:
                    conductor._run_worker(worker, getattr(conductor, "_" + worker.replace("-", "_") + "_worker"))
                self.assertEqual(conductor.errors, [])
                self.assertEqual(operation.call_count, 2)
                self.assertEqual(project.call_count, int(worker == "network"))
                self.assertEqual(len(conductor.warnings), 1)
                self.assertFalse(store.current()["latest"]["worker.error"]["payload"]["fatal"])

    def test_full_verification_queue_never_marks_unsent_clients_pending(self):
        conductor, store = self._conductor()
        conductor.action_attempts = 100
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

        with patch("room_service.conductor.load_policy", return_value=policy.config), \
             patch("room_service.conductor._simulated_bss_channels", return_value={}), \
             patch("room_service.conductor.ThresholdPolicy", return_value=policy), \
             patch("room_service.conductor.PrplMeshCandidateProvider", return_value=provider), \
             patch("room_service.conductor.StreamingCandidateProvider", return_value=provider), \
             patch("room_service.conductor.PrplMeshObserver", return_value=observer), \
             patch("room_service.conductor.SteerActuator", return_value=actuator), \
             patch.object(policy, "evaluate", wraps=policy.evaluate) as evaluate, \
             patch.object(conductor._verification_executor, "submit", side_effect=futures), \
             patch.object(conductor, "_optimizer_wait", side_effect=advance):
            conductor._optimizer_worker()
        self.assertEqual(conductor.errors, [])
        self.assertEqual(waits, [5, 5, 6])
        self.assertEqual(conductor.action_attempts, 106)
        self.assertIsNone(store.current()["optimizer"]["maximum_actions"])
        queued_state = evaluate.call_args_list[2].args[1]
        for client in clients[5:]:
            pending = queued_state.for_sta(client.sta_mac)
            self.assertEqual(pending.phase, "holding")
            self.assertIsNone(pending.pending_since)
            self.assertIsNone(pending.last_action_at)
            self.assertEqual(pending.failure_count, 0)
        self.assertEqual(actuator.execute.call_args_list[5].args[0].sta_mac, clients[5].sta_mac)

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

    def test_health_uses_prplmesh_nodes_not_rdk_database_counts(self):
        conductor, store = self._conductor()
        conductor.manifest["health"] = {"interval_seconds": 1,
            "expected_mesh_devices": 5, "expected_clients": 20}
        for clients, expected in ((20, True), (19, False)):
            with patch.object(conductor, "_wait_for_run", return_value=True), \
                 patch.object(conductor, "_active", return_value=True), \
                 patch.object(conductor, "_sleep", return_value=True), \
                 patch("room_service.conductor.mesh_health", return_value={
                     "api_active": clients, "topology_nodes": 5, "complete_nodes": 5}):
                conductor._health_worker()
            self.assertEqual(store.current()["health"]["healthy"], expected)

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
                    "radio_permanent_mac": "42:00:00:00:11:00",
                    "station_mac": "02:00:00:10:02:00",
                    "container": "prpl-client-03",
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
                }
            }
        }
        manifest = {
            "hero": {"role": "sta_mobile_01"},
            "traffic": {"target": "192.168.77.1", "timeout_seconds": 1},
        }
        return LiveConductor(
            store, plan, manifest, mode="recommend", repo_root=Path(temp.name)
        ), store

    def test_closed_action_window_preserves_holding_without_false_pending(self):
        sta = "02:00:00:10:02:00"
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

    def test_controller_observation_is_projected_to_world_role(self):
        conductor, _store = self._conductor()
        snapshot = Snapshot(
            schema_version=1,
            sequence=0,
            observed_at="2026-09-03T00:00:00Z",
            controller_url="http://controller",
            health=MeshHealth(devices=5, clients=1, bsses=50),
            clients=(ClientObservation(
                sta_mac="02:00:00:10:02:00",
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
        self.assertEqual(payload["hero"]["display_name"], "sta-02")
        self.assertEqual(payload["hero"]["connected_role"], "extender_1")
        self.assertEqual(payload["hero"]["connected_world_name"], "Extender-1")
        self.assertEqual(payload["hero"]["rssi_dbm"], -41)
        self.assertEqual(payload["cohorts"], {"private": 1, "iot": 0, "other": 0})

    def test_hero_uses_controller_station_mac_not_permanent_radio_mac(self):
        conductor, _store = self._conductor()

        self.assertEqual(conductor.hero_mac, "02:00:00:10:02:00")
        self.assertEqual(conductor.hero_container, "prpl-client-03")


if __name__ == "__main__":
    unittest.main()
