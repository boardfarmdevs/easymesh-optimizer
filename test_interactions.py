from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from wmdcfg.actuator import ActuatorError, DaemonStatus
from room_demo.engine import RoomEngine
from room_demo.events import EventStore
from room_demo.interactions import InteractionError, InteractiveMediumSession
from room_demo.recovery import RecoveryJournal, load_recovery, recover_medium


WORLD = {
    "schema": "wmdcfg.world-plan.v1",
    "name": "interactive-test",
    "duration_ms": 60_000,
    "tick_ms": 1000,
    "roles": {
        "gateway": "fronthaul_ap",
        "extender_1": "fronthaul_ap",
        "sta_01": "station",
    },
    "generations": [{
        "time_ms": 0,
        "positions": {"gateway": [1, 2], "extender_1": [9, 2], "sta_01": [2, 2]},
        "present": {"gateway": True, "extender_1": True, "sta_01": True},
        "links": [],
    }],
}

LAYOUT = {
    "schema": "wmdcfg.world-layout.v1",
    "name": "interactive-test",
    "space": {"width_m": 10, "height_m": 6},
    "propagation": {
        "reference_distance_m": 1,
        "reference_snr_db_by_band": {"2.4": 54, "5": 50, "6": 47},
        "path_loss_exponent": 2,
        "shadowing_stddev_db": 0,
        "minimum_snr_db": -20,
        "maximum_snr_db": 60,
    },
    "nodes": [
        {"role": "gateway", "kind": "fronthaul_ap", "position": [1, 2]},
        {"role": "extender_1", "kind": "fronthaul_ap", "position": [9, 2]},
    ],
    "walls": [{"name": "partition", "start": [5, 0], "end": [5, 6], "loss_db": 5}],
}

PLAN = {
    "bindings": {
        "gateway": {
            "role_type": "fronthaul_ap", "radio_tx_mac": "02:00:00:00:01:00",
            "fronthaul_frequencies_mhz": {"5": 5180},
            "band_radios": {"5": {"tx_mac": "02:00:00:00:01:20"}},
        },
        "extender_1": {
            "role_type": "fronthaul_ap", "radio_tx_mac": "02:00:00:00:02:00",
            "fronthaul_frequencies_mhz": {"5": 5180},
            "band_radios": {"5": {"tx_mac": "02:00:00:00:02:20"}},
        },
        "sta_01": {
            "role_type": "station", "radio_tx_mac": "02:00:00:00:03:00",
            "radio_permanent_mac": "02:00:00:00:03:00", "container": "client",
        },
    },
}


class FakeClient:
    def __init__(self, _path):
        self.generation = 4
        self.closed = False
        self.values = {}
        self.applied = []
        self.thread_ids = set()

    def _trace(self):
        self.thread_ids.add(threading.get_ident())

    def _status(self):
        return DaemonStatus(
            "instance", self.generation,
            frozenset({
                "atomic_generations", "readback", "dump_links",
                "frequency_qualified_snr",
            }),
            1024, 3,
        )

    def connect(self):
        self._trace()
        return self._status()

    def status(self):
        self._trace()
        return self._status()

    def close(self):
        self._trace()
        self.closed = True

    def get_frequency_link(self, source, destination, frequency):
        self._trace()
        value, overridden = self.values.get((source, destination, frequency), (31, False))
        return self.generation, value, overridden

    def apply_frequency(self, generation, updates):
        self._trace()
        if generation != self.generation + 1:
            raise ActuatorError("generation conflict")
        self.generation = generation
        normalized = []
        for item in updates:
            row = {
                "source": item["source"], "destination": item["destination"],
                "frequency_mhz": int(item["frequency_mhz"]),
                "value": int(item.get("value", 0)),
                "override": bool(item.get("override", True)),
            }
            key = (row["source"], row["destination"], row["frequency_mhz"])
            if row["override"]:
                self.values[key] = (row["value"], True)
            else:
                self.values.pop(key, None)
            normalized.append(row)
        self.applied.append(normalized)
        return normalized


class InteractiveMediumSessionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = EventStore(
            "interactive-1", WORLD, Path(temp.name) / "events.jsonl"
        )
        self.client = FakeClient("fake")
        self.session = InteractiveMediumSession(
            self.store, WORLD, LAYOUT, PLAN, "fake",
            client_factory=lambda _path: self.client,
            minimum_update_interval=0,
        )
        self.session.start()
        self.addCleanup(self.session.close)
        self.lease = self.session.acquire("browser-test")

    def test_position_is_atomic_revisioned_and_frequency_qualified(self):
        # The initial room is complete: four station/AP directions plus both
        # directions of the gateway/extender backhaul in this one-band fixture.
        self.assertEqual(len(self.client.applied[0]), 6)
        result = self.session.position(
            "sta_01", token=self.lease["token"], expected_revision=0,
            position=[8, 2], final=True, client_sequence=7,
        )
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["position"], [8.0, 2.0])
        self.assertEqual(result["changed_link_count"], 4)
        self.assertEqual({item["frequency_mhz"] for item in self.client.applied[-1]}, {5180})
        self.assertEqual(self.client.applied[0][0]["override"], True)
        self.assertEqual(self.session.snapshot()["last_rf_role"], "sta_01")
        by_ap = {item["ap_role"]: item for item in result["links"]}
        self.assertEqual(by_ap["extender_1"]["snr_db"], 50)
        self.assertLess(by_ap["gateway"]["snr_db"], 50)
        with self.assertRaisesRegex(InteractionError, "current revision is 1"):
            self.session.position(
                "sta_01", token=self.lease["token"], expected_revision=0,
                position=[7, 2], final=True,
            )

    def test_same_quantized_position_commits_without_an_rf_generation(self):
        generation = self.client.generation
        apply_count = len(self.client.applied)
        result = self.session.position(
            "sta_01", token=self.lease["token"], expected_revision=0,
            position=[2.01, 2.01], final=True,
        )
        self.assertEqual(result["position"], [2.0, 2.0])
        self.assertEqual(result["changed_link_count"], 0)
        self.assertEqual(self.client.generation, generation)
        self.assertEqual(len(self.client.applied), apply_count)
        self.assertEqual(self.store.all()[-1]["kind"], "rf.generation.noop")

    def test_external_generation_contaminates_without_committing_position(self):
        self.client.generation += 1
        with self.assertRaisesRegex(ActuatorError, "unexpected external medium change"):
            self.session.position(
                "sta_01", token=self.lease["token"], expected_revision=0,
                position=[8, 2], final=True,
            )
        snapshot = self.session.snapshot()
        self.assertEqual(snapshot["revision"], 0)
        self.assertEqual(snapshot["roles"]["sta_01"]["position"], [2.0, 2.0])
        self.assertIn("unexpected external medium change", snapshot["fault"])
        self.assertIn(
            "medium.external_write_detected",
            [item["kind"] for item in self.store.all()],
        )
        self.assertFalse(self.session.close())

    def test_presence_uses_the_minimum_snr_and_release_freezes_state(self):
        result = self.session.presence(
            "sta_01", token=self.lease["token"], expected_revision=0,
            present=False,
        )
        self.assertFalse(result["present"])
        self.assertTrue(all(item["snr_db"] == -20 for item in result["links"]))
        self.assertTrue(all(item["value"] == -20 for item in self.client.applied[-1]))
        self.session.release(self.lease["token"])
        self.assertFalse(self.session.snapshot()["lease"]["held"])
        self.assertFalse(self.session.snapshot()["roles"]["sta_01"]["present"])

    def test_extender_fronthaul_can_disappear_and_reappear_atomically(self):
        initial_generation = self.client.generation
        snapshot = self.session.snapshot()
        self.assertIn("extender_1", snapshot["presence_roles"])
        self.assertIn("extender_1", snapshot["movable_roles"])
        self.assertNotIn("gateway", snapshot["presence_roles"])
        self.assertIn("gateway", snapshot["movable_roles"])

        absent = self.session.presence(
            "extender_1", token=self.lease["token"], expected_revision=0,
            present=False,
        )
        self.assertFalse(absent["present"])
        self.assertEqual(absent["changed_link_count"], 2)
        self.assertTrue(all(item["snr_db"] == -20 for item in absent["links"]))
        self.assertTrue(all(item["value"] == -20 for item in self.client.applied[-1]))
        self.assertEqual(self.client.generation, initial_generation + 1)

        restored = self.session.presence(
            "extender_1", token=self.lease["token"], expected_revision=1,
            present=True,
        )
        self.assertTrue(restored["present"])
        self.assertEqual(restored["changed_link_count"], 2)
        self.assertTrue(all(item["value"] > -20 for item in self.client.applied[-1]))
        self.assertEqual(self.client.generation, initial_generation + 2)

        committed = [
            item for item in self.store.all()
            if item["kind"] == "room.presence.committed"
        ]
        self.assertEqual([item["payload"]["role"] for item in committed], [
            "extender_1", "extender_1",
        ])

    def test_extender_move_updates_fronthaul_and_backhaul_atomically(self):
        initial_generation = self.client.generation
        result = self.session.position(
            "extender_1", token=self.lease["token"], expected_revision=0,
            position=[8, 4], final=True,
        )
        self.assertEqual(result["position"], [8.0, 4.0])
        self.assertEqual(result["changed_link_count"], 4)
        self.assertEqual(self.client.generation, initial_generation + 1)
        self.assertEqual(
            {item["link_class"] for item in result["links"]},
            {"fronthaul", "backhaul"},
        )
        self.assertEqual(len(self.client.applied[-1]), 4)
        backhaul = [
            item for item in result["links"]
            if item["link_class"] == "backhaul"
        ]
        self.assertEqual(backhaul[0]["peer_role"], "gateway")
        self.assertEqual(backhaul[0]["band"], "5")

    def test_colocated_gateway_agent_is_movable_but_cannot_disappear(self):
        initial_generation = self.client.generation
        result = self.session.position(
            "gateway", token=self.lease["token"], expected_revision=0,
            position=[3, 4], final=True,
        )
        self.assertEqual(result["position"], [3.0, 4.0])
        self.assertEqual(result["changed_link_count"], 4)
        self.assertEqual(self.client.generation, initial_generation + 1)
        self.assertEqual(
            {item["link_class"] for item in result["links"]},
            {"fronthaul", "backhaul"},
        )
        with self.assertRaisesRegex(InteractionError, "not interactive"):
            self.session.presence(
                "gateway", token=self.lease["token"], expected_revision=1,
                present=False,
            )

    def test_wrong_lease_and_non_station_are_rejected(self):
        with self.assertRaisesRegex(InteractionError, "does not match"):
            self.session.position(
                "sta_01", token="wrong", expected_revision=0,
                position=[4, 2], final=True,
            )
        with self.assertRaisesRegex(InteractionError, "not interactive"):
            self.session.presence(
                "gateway", token=self.lease["token"], expected_revision=0,
                present=False,
            )

    def test_close_restores_exact_override_state(self):
        self.session.position(
            "sta_01", token=self.lease["token"], expected_revision=0,
            position=[8, 2], final=True,
        )
        self.assertTrue(any(value[1] for value in self.client.values.values()))
        self.assertTrue(self.session.close())
        self.assertEqual(self.client.values, {})
        self.assertTrue(self.client.closed)
        self.assertTrue(self.store.current()["restored"])

    def test_steering_assist_is_atomic_and_restores_room_matrix(self):
        before_generation = self.client.generation
        before_values = dict(self.client.values)
        before_epoch = self.session.snapshot()["environment_epoch"]
        before_measurement_epoch = self.session.snapshot()["measurement_epoch"]
        observed = {}

        def action():
            observed["values"] = dict(self.client.values)
            return "btm-submitted"

        result = self.session.steering_action(
            "sta_01", "gateway", "extender_1", "5", action
        )

        self.assertEqual(result, "btm-submitted")
        self.assertEqual(self.client.generation, before_generation + 2)
        self.assertEqual(self.client.values, before_values)
        self.assertEqual(
            observed["values"][(
                "02:00:00:00:02:20", "02:00:00:00:03:00", 5180
            )],
            (60, True),
        )
        self.assertEqual(
            observed["values"][(
                "02:00:00:00:03:00", "02:00:00:00:02:20", 5180
            )],
            (60, True),
        )
        self.assertEqual(
            observed["values"][(
                "02:00:00:00:01:20", "02:00:00:00:03:00", 5180
            )],
            (20, True),
        )
        snapshot = self.session.snapshot()
        self.assertEqual(snapshot["environment_epoch"], before_epoch)
        self.assertEqual(
            snapshot["measurement_epoch"], before_measurement_epoch + 1
        )
        self.assertEqual(snapshot["last_rf_role"], "sta_01")
        events = self.store.all()
        self.assertIn("rf.steering_assist.started", [item["kind"] for item in events])
        completed = next(
            item for item in events
            if item["kind"] == "rf.steering_assist.completed"
        )
        self.assertTrue(completed["payload"]["room_matrix_restored"])
        self.assertEqual(completed["payload"]["environment_epoch"], before_epoch)
        self.assertEqual(
            completed["payload"]["measurement_epoch"],
            before_measurement_epoch + 1,
        )

    def test_steering_assist_restores_after_action_failure(self):
        before_values = dict(self.client.values)

        def action():
            raise RuntimeError("BTM failed")

        with self.assertRaisesRegex(RuntimeError, "BTM failed"):
            self.session.steering_action(
                "sta_01", "gateway", "extender_1", "5", action
            )
        self.assertEqual(self.client.values, before_values)
        completed = next(
            item for item in self.store.all()
            if item["kind"] == "rf.steering_assist.completed"
        )
        self.assertTrue(completed["payload"]["room_matrix_restored"])
        self.assertEqual(completed["payload"]["error"], "BTM failed")

    def test_runtime_world_carries_source_geometry(self):
        runtime = InteractiveMediumSession.runtime_world(WORLD, LAYOUT)
        self.assertEqual(runtime["space"], LAYOUT["space"])
        self.assertEqual(runtime["propagation"], LAYOUT["propagation"])
        self.assertTrue(runtime["interaction"]["authoritative"])

    def test_server_owned_movement_completes_at_the_destination(self):
        started = self.session.move(
            "sta_01", token=self.lease["token"], expected_revision=0,
            destination=[2.2, 2], speed_mps=10, client_sequence=11,
        )
        self.assertEqual(started["revision"], 1)
        self.assertEqual(started["movement"]["status"], "running")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            movement = self.session.snapshot()["movements"][-1]
            if movement["status"] == "completed":
                break
            time.sleep(0.02)
        self.assertEqual(movement["status"], "completed")
        self.assertEqual(movement["position"], [2.2, 2.0])
        self.assertEqual(self.session.snapshot()["roles"]["sta_01"]["position"], [2.2, 2.0])
        kinds = [item["kind"] for item in self.store.all()]
        self.assertIn("interaction.movement.started", kinds)
        self.assertIn("interaction.movement.completed", kinds)

    def test_releasing_lease_cancels_a_server_owned_movement(self):
        started = self.session.move(
            "sta_01", token=self.lease["token"], expected_revision=0,
            destination=[8, 2], speed_mps=0.1,
        )
        self.session.release(self.lease["token"])
        movement = next(
            item for item in self.session.snapshot()["movements"]
            if item["id"] == started["movement"]["id"]
        )
        self.assertEqual(movement["status"], "cancelled")
        self.assertEqual(movement["reason"], "lease_released")

    def test_server_owned_movement_can_pause_resume_and_cancel(self):
        started = self.session.move(
            "sta_01", token=self.lease["token"], expected_revision=0,
            destination=[8, 2], speed_mps=0.1,
        )
        movement_id = started["movement"]["id"]
        paused = self.session.movement_control(
            movement_id, "pause", token=self.lease["token"],
            expected_revision=started["revision"],
        )
        self.assertEqual(paused["movement"]["status"], "paused")
        position = self.session.snapshot()["roles"]["sta_01"]["position"]
        time.sleep(0.25)
        self.assertEqual(
            self.session.snapshot()["roles"]["sta_01"]["position"], position
        )
        resumed = self.session.movement_control(
            movement_id, "resume", token=self.lease["token"],
            expected_revision=paused["revision"],
        )
        self.assertEqual(resumed["movement"]["status"], "running")
        cancelled = self.session.movement_control(
            movement_id, "cancel", token=self.lease["token"],
            expected_revision=resumed["revision"],
        )
        self.assertEqual(cancelled["movement"]["status"], "cancelled")

    def test_recording_exports_runnable_mobility_and_world_documents(self):
        started = self.session.start_recording(
            token=self.lease["token"], expected_revision=0, name="my room walk"
        )
        self.assertTrue(started["recording"]["active"])
        time.sleep(0.01)
        self.session.position(
            "sta_01", token=self.lease["token"], expected_revision=0,
            position=[8, 2], final=True,
        )
        time.sleep(0.01)
        self.session.presence(
            "sta_01", token=self.lease["token"], expected_revision=1,
            present=False,
        )
        stopped = self.session.stop_recording(
            token=self.lease["token"], expected_revision=2
        )
        self.assertTrue(stopped["recording"]["export_ready"])
        mobility, world = self.session.recorded_documents()
        self.assertEqual(mobility["schema"], "wmdcfg.mobility.v1")
        self.assertEqual(mobility["name"], "my-room-walk")
        self.assertEqual(world["schema"], "wmdcfg.world-plan.v1")
        self.assertEqual(self.session.recorded_world(), world)
        station = next(item for item in mobility["nodes"] if item["role"] == "sta_01")
        self.assertEqual(station["path"][-1]["position"], [8.0, 2.0])
        self.assertNotEqual(station["presence"], [[0, mobility["duration_ms"]]])

    def test_recording_preserves_colocated_gateway_movement(self):
        self.session.position(
            "gateway", token=self.lease["token"], expected_revision=0,
            position=[2, 3], final=True,
        )
        self.session.start_recording(
            token=self.lease["token"], expected_revision=1,
            name="gateway movement",
        )
        time.sleep(0.01)
        generation = self.client.generation
        moved = self.session.position(
            "gateway", token=self.lease["token"], expected_revision=1,
            position=[3, 4], final=True,
        )
        self.assertEqual(moved["daemon_generation"], generation + 1)
        self.assertIsNone(self.session.snapshot()["fault"])
        time.sleep(0.21)
        self.session.stop_recording(
            token=self.lease["token"], expected_revision=2,
        )
        mobility, world = self.session.recorded_documents()
        self.assertEqual(
            {node["role"] for node in mobility["nodes"]}, set(WORLD["roles"])
        )
        gateway = next(
            node for node in mobility["nodes"] if node["role"] == "gateway"
        )
        self.assertEqual(gateway["path"][0]["position"], [2.0, 3.0])
        self.assertEqual(gateway["path"][-1]["position"], [3.0, 4.0])
        self.assertNotIn("presence", gateway)
        self.assertEqual(
            world["generations"][-1]["positions"]["gateway"], [3.0, 4.0]
        )
        self.assertTrue(self.session.close())
        self.assertEqual(self.client.values, {})

    def test_recording_preserves_an_extender_fronthaul_outage(self):
        self.session.start_recording(
            token=self.lease["token"], expected_revision=0,
            name="extender outage",
        )
        time.sleep(0.01)
        self.session.presence(
            "extender_1", token=self.lease["token"], expected_revision=0,
            present=False,
        )
        time.sleep(0.01)
        self.session.presence(
            "extender_1", token=self.lease["token"], expected_revision=1,
            present=True,
        )
        self.session.stop_recording(
            token=self.lease["token"], expected_revision=2,
        )
        mobility, world = self.session.recorded_documents()
        extender = next(
            item for item in mobility["nodes"] if item["role"] == "extender_1"
        )
        self.assertIn("presence", extender)
        self.assertNotEqual(
            extender["presence"], [[0, mobility["duration_ms"]]]
        )
        self.assertEqual(world["schema"], "wmdcfg.world-plan.v1")


class RoomEngineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = EventStore(
            "engine-1", WORLD, Path(temp.name) / "events.jsonl"
        )
        self.client = FakeClient("fake")
        session = InteractiveMediumSession(
            self.store, WORLD, LAYOUT, PLAN, "fake",
            client_factory=lambda _path: self.client,
            minimum_update_interval=0,
        )
        self.engine = RoomEngine(session)

    def test_all_medium_operations_run_on_one_actor_thread(self):
        caller = threading.get_ident()
        self.engine.start()
        lease = self.engine.acquire(
            "browser-test", command_id="test-acquire-001"
        )
        self.engine.position(
            "sta_01", token=lease["token"], expected_revision=0,
            position=[8, 2], final=True, command_id="test-position-001",
        )
        self.assertTrue(self.engine.close())
        self.assertEqual(len(self.client.thread_ids), 1)
        self.assertNotIn(caller, self.client.thread_ids)

    def test_autonomous_movement_uses_the_same_actor(self):
        self.engine.start()
        lease = self.engine.acquire(
            "browser-test", command_id="test-acquire-002"
        )
        started = self.engine.move(
            "sta_01", token=lease["token"], expected_revision=0,
            destination=[2.2, 2], speed_mps=10,
            command_id="test-movement-001",
        )
        deadline = time.monotonic() + 2
        movement = started["movement"]
        while time.monotonic() < deadline:
            movement = self.engine.snapshot()["movements"][-1]
            if movement["status"] == "completed":
                break
            time.sleep(0.02)
        self.assertEqual(movement["status"], "completed")
        self.assertTrue(self.engine.close())
        self.assertEqual(len(self.client.thread_ids), 1)

    def test_steering_transaction_runs_on_the_medium_actor(self):
        caller = threading.get_ident()
        action_threads = []
        self.engine.start()

        result = self.engine.steering_action(
            "sta_01", "gateway", "extender_1", "5",
            lambda: action_threads.append(threading.get_ident()) or "submitted",
        )

        self.assertEqual(result, "submitted")
        self.assertEqual(len(action_threads), 1)
        self.assertNotEqual(action_threads[0], caller)
        self.assertIn(action_threads[0], self.client.thread_ids)
        self.assertTrue(self.engine.close())

    def test_close_during_movement_does_not_deadlock_or_accept_late_work(self):
        self.engine.start()
        lease = self.engine.acquire(
            "browser-test", command_id="test-acquire-003"
        )
        self.engine.move(
            "sta_01", token=lease["token"], expected_revision=0,
            destination=[8, 2], speed_mps=0.1,
            command_id="test-movement-002",
        )
        started = time.monotonic()
        self.assertTrue(self.engine.close())
        self.assertLess(time.monotonic() - started, 1.0)
        with self.assertRaisesRegex(RuntimeError, "room engine is closing"):
            self.engine.position(
                "sta_01", token=lease["token"], expected_revision=1,
                position=[7, 2], final=True, command_id="test-position-002",
            )

    def test_final_snapshot_is_cached_and_defensively_copied(self):
        self.engine.start()
        self.assertTrue(self.engine.close())
        first = self.engine.snapshot()
        first["roles"]["sta_01"]["position"][0] = 99
        self.assertEqual(
            self.engine.snapshot()["roles"]["sta_01"]["position"],
            [2.0, 2.0],
        )

    def test_close_before_start_has_nothing_to_restore(self):
        self.assertTrue(self.engine.close())
        self.assertTrue(self.engine.snapshot()["restored"])
        self.assertFalse(self.client.closed)

    def test_duplicate_command_returns_original_result_without_second_write(self):
        self.engine.start()
        lease = self.engine.acquire(
            "browser-test", command_id="test-acquire-004"
        )
        kwargs = {
            "token": lease["token"],
            "expected_revision": 0,
            "position": [8, 2],
            "final": True,
            "command_id": "test-position-003",
        }
        first = self.engine.position("sta_01", **kwargs)
        apply_count = len(self.client.applied)
        second = self.engine.position("sta_01", **kwargs)
        self.assertEqual(second, first)
        self.assertEqual(len(self.client.applied), apply_count)
        with self.assertRaisesRegex(InteractionError, "different operation"):
            self.engine.position(
                "sta_01", token=lease["token"], expected_revision=1,
                position=[7, 2], final=True,
                command_id="test-position-003",
            )
        self.assertTrue(self.engine.close())


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "recovery.json"
        self.store = EventStore("recovery-1", WORLD, self.root / "events.jsonl")
        self.client = FakeClient("fake")

    def test_interrupted_session_can_restore_its_exact_baseline(self):
        recovery = RecoveryJournal(self.path, "recovery-1", "inventory-hash")
        session = InteractiveMediumSession(
            self.store, WORLD, LAYOUT, PLAN, "fake",
            client_factory=lambda _path: self.client,
            minimum_update_interval=0,
            recovery=recovery,
        )
        session.start()
        lease = session.acquire("browser-test")
        session.position(
            "sta_01", token=lease["token"], expected_revision=0,
            position=[8, 2], final=True,
        )
        active = load_recovery(self.path)
        self.assertEqual(active["state"], "active")
        self.assertEqual(active["last_committed_generation"], self.client.generation)

        # Simulate a worker that disappeared without entering normal close().
        session._client.close()
        session._client = None
        result = recover_medium(
            self.path, "fake", client_factory=lambda _path: self.client
        )
        self.assertEqual(result["status"], "restored")
        self.assertEqual(self.client.values, {})
        self.assertEqual(load_recovery(self.path)["state"], "restored")

    def test_incomplete_record_blocks_a_second_session(self):
        first = RecoveryJournal(self.path, "first", "inventory-hash")
        first.prepare("instance", 4, {
            ("02:00:00:00:01:00", "02:00:00:00:02:00", 5180): (31, False)
        })
        second = RecoveryJournal(self.path, "second", "inventory-hash")
        with self.assertRaisesRegex(ActuatorError, "run room-demo recover first"):
            second.prepare("instance", 4, {})


if __name__ == "__main__":
    unittest.main()
