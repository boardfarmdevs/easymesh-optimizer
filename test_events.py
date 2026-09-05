from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from room_demo.events import EventStore


WORLD = {"name": "test-world", "duration_ms": 1000, "tick_ms": 100}


def event(sequence: int, kind: str, **payload):
    return {
        "schema": "easymesh.room-demo.event.v1",
        "run_id": "run-1",
        "sequence": sequence,
        "recorded_at": "2026-09-03T00:00:00+00:00",
        "world_time_ms": min(sequence * 100, 1000),
        "kind": kind,
        "payload": payload,
    }


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "live-events.jsonl"
        self.store = EventStore("run-1", WORLD, self.path)

    def test_state_follows_runner_and_persists_events(self):
        self.store.publish(event(1, "runner.preflight", health={}))
        self.store.publish(event(2, "scenario.started"))
        self.store.publish(event(3, "rf.restore.completed", verified=True))
        self.store.publish(event(4, "scenario.completed", outcome="passed",
                                 restored=True, error=None, run_directory="/tmp/run-1"))
        current = self.store.current()
        self.assertEqual(current["state"], "passed")
        self.assertTrue(current["restored"])
        self.assertEqual(current["sequence"], 4)
        self.assertEqual(len(self.path.read_text().splitlines()), 4)

    def test_replay_is_strictly_after_sequence(self):
        self.store.publish(event(1, "scenario.started"))
        self.store.publish(event(2, "scenario.clock"))
        self.assertEqual([item["sequence"] for item in self.store.after(1)], [2])

    def test_measurement_outage_replaces_old_convergence_until_recovered(self):
        self.store.emit("optimizer.evaluation", 0, {"fleet": {"converged": True}})
        unavailable = {"status": "unavailable", "automatic_actuation_ready": False}
        self.store.emit("optimizer.measurement.unavailable", 100, unavailable)
        self.assertEqual(self.store.current()["optimizer"], unavailable)
        self.store.emit("optimizer.evaluation", 200, {"fleet": {"converged": False}})
        self.assertNotIn("status", self.store.current()["optimizer"])

    def test_rejects_out_of_order_event(self):
        self.store.publish(event(1, "scenario.started"))
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            self.store.publish(event(1, "scenario.clock"))

    def test_multiple_producers_are_centrally_sequenced(self):
        first = self.store.ingest(event(80, "scenario.clock"))
        second = self.store.emit(
            "network.snapshot", 100, {"clients": []}, producer="network"
        )
        self.assertEqual(first["sequence"], 1)
        self.assertEqual(first["payload"]["producer_sequence"], 80)
        self.assertEqual(second["sequence"], 2)
        self.assertEqual(self.store.current()["latest"]["network.snapshot"], second)

    def test_emitted_events_are_hash_chained_and_have_separate_clocks(self):
        first = self.store.emit("runner.preflight", 100, {})
        second = self.store.emit("scenario.clock", 200, {})
        self.assertEqual(first["scenario_time_ms"], 100)
        self.assertGreaterEqual(second["run_elapsed_ms"], first["run_elapsed_ms"])
        self.assertEqual(second["previous_event_hash"], first["event_hash"])
        self.assertEqual(self.store.current()["evidence_digest"], second["event_hash"])
        self.assertIsNotNone(self.store.current()["state_digest"])

    def test_reducer_reconstructs_role_medium_and_environment_state(self):
        world = {
            "name": "room", "duration_ms": 1000, "tick_ms": 100,
            "roles": {"sta_01": "station"},
            "generations": [{
                "positions": {"sta_01": [1, 2]},
                "present": {"sta_01": True},
            }],
        }
        store = EventStore("run-1", world, self.path)
        store.emit("room.position.committed", 100, {
            "revision": 3,
            "environment_epoch": 2,
            "role": "sta_01",
            "position": [4, 5],
            "present": True,
        })
        store.emit("rf.generation.applied", 100, {
            "revision": 3,
            "environment_epoch": 2,
            "daemon_instance_id": "medium-1",
            "daemon_generation": 17,
            "changed_link_count": 6,
        })
        current = store.current()
        self.assertEqual(current["schema"], "easymesh.room-demo.state.v2")
        self.assertEqual(current["world_revision"], 3)
        self.assertEqual(current["environment_epoch"], 2)
        self.assertEqual(
            current["roles"]["sta_01"]["authoritative_position"], [4, 5]
        )
        self.assertEqual(current["medium"]["generation"], 17)

    def test_completed_evidence_can_be_loaded_without_rewriting_it(self):
        self.store.publish(event(1, "scenario.started"))
        run_dir = self.path.parent
        (run_dir / "world.json").write_text(__import__("json").dumps(WORLD))
        before = self.path.read_text()
        loaded = EventStore.from_evidence(run_dir)
        self.assertEqual(loaded.all()[0]["kind"], "scenario.started")
        self.assertEqual(self.path.read_text(), before)

    def test_tampered_hashed_event_is_rejected(self):
        emitted = self.store.emit("scenario.clock", 100, {})
        tampered = dict(emitted)
        tampered["sequence"] = 2
        tampered["payload"] = {"changed": True}
        with self.assertRaisesRegex(ValueError, "event hash"):
            self.store.publish(tampered)


if __name__ == "__main__":
    unittest.main()
