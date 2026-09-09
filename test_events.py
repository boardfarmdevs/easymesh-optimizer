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
    def test_probe_selection_clears_old_sample_and_ignores_late_old_identity(self):
        old = {"role": "sta_old", "selection": 0}
        selected = {"role": "sta_new", "selection": 1}
        self.store.emit("traffic.sample", 0, {"traffic_probe": old, "success": True})
        self.store.emit("traffic.probe.selected", 0, {"traffic_probe": selected, "revision": 1})
        self.assertNotIn("traffic.sample", self.store.current()["latest"])
        self.store.emit("traffic.sample", 0, {"traffic_probe": old, "success": True})
        self.assertNotIn("traffic.sample", self.store.current()["latest"])
        self.store.emit("network.snapshot", 0, {"traffic_probe": old,
            "clients": [{"role": "sta_old"}, {"role": "sta_new"}], "hero": {"role": "sta_old"}})
        self.assertEqual(self.store.current()["traffic_probe"], selected)
        self.assertEqual(self.store.current()["network"]["hero"]["role"], "sta_new")

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

    def test_incremental_replay_uses_an_index_instead_of_scanning_the_journal(self):
        for sequence in (1, 3, 9):
            self.store.publish(event(sequence, "scenario.clock"))
        class IndexedOnly(list):
            def __iter__(self):
                raise AssertionError("the entire event history was scanned")
        self.store._events = IndexedOnly(self.store._events)
        self.assertEqual([item["sequence"] for item in self.store.after(3)], [9])
        self.assertEqual([item["sequence"] for item in self.store.wait_after(2, 0)], [3, 9])
        self.assertEqual(self.store.wait_after(9, 0), [])

    def test_measurement_outage_replaces_old_convergence_until_recovered(self):
        self.store.emit("optimizer.evaluation", 0, {"fleet": {"converged": True}})
        unavailable = {"status": "unavailable", "automatic_actuation_ready": False}
        self.store.emit("optimizer.measurement.unavailable", 100, unavailable)
        self.assertEqual(self.store.current()["optimizer"], unavailable)

    def test_rf_changes_invalidate_old_convergence_and_progress_survives_reload(self):
        self.store.emit("optimizer.evaluation", 0, {"fleet": {"converged": True}, "client_decisions": [{"reason": "ready"}]})
        self.store.emit("optimizer.environment.changed", 0, {"environment_epoch": 2})
        current = self.store.current()["optimizer"]
        self.assertEqual(current["fleet"], {})
        self.assertEqual(current["client_decisions"], [])
        self.assertFalse(current["automatic_actuation_ready"])
        self.store.emit("optimizer.progress", 0, {"phase": "candidate_queries", "completed_queries": 2, "total_queries": 5})
        self.assertEqual(self.store.current()["optimizer"]["progress"]["completed_queries"], 2)
        self.store.emit("optimizer.evaluation", 0, {"evaluated_at": "2026-09-07T03:00:00Z"})
        self.assertNotIn("progress", self.store.current()["optimizer"])
        self.store.emit("optimizer.evaluation", 200, {"fleet": {"converged": False}})
        self.assertNotIn("status", self.store.current()["optimizer"])

    def test_verified_subject_leaves_pending_without_another_candidate_round(self):
        self.store.emit('optimizer.evaluation', 0, {'subject_role': 'station', 'policy_state': {'phase': 'pending'},
                                                   'fleet': {'converged': False}, 'decision': {'action': 'steer'}})
        self.store.emit('optimizer.verification', 0, {'subject_role': 'station', 'success': True,
                                                     'policy_state': {'phase': 'cooldown'}})
        optimizer = self.store.current()['optimizer']
        self.assertEqual(optimizer['policy_state']['phase'], 'cooldown')
        self.assertEqual(optimizer['decision']['action'], 'none')
        self.assertFalse(optimizer['fleet']['converged'])

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
