from __future__ import annotations

import copy
from contextlib import nullcontext
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from room_demo.engine import RoomEngine
from room_demo.events import EventStore
from room_demo.interactions import InteractionError, InteractiveMediumSession
from wmdcfg.actuator import ActuatorError

from test_interactions import FakeClient, LAYOUT, PLAN, WORLD


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.world = copy.deepcopy(WORLD)
        self.world["duration_ms"] = 3000
        for elapsed, client_position, extender_position in ((1000, [3, 2], [8, 2]),
                                                            (2000, [4, 2], [7, 2]),
                                                            (3000, [5, 2], [6, 2])):
            frame = copy.deepcopy(self.world["generations"][0])
            frame["time_ms"] = elapsed
            frame["positions"].update(sta_01=client_position, extender_1=extender_position)
            self.world["generations"].append(frame)
        self.client = FakeClient("unused")
        self.store = EventStore("playback", self.world, Path(self.temp.name) / "events.jsonl")
        self.session = InteractiveMediumSession(self.store, self.world, LAYOUT, PLAN, "unused",
                                              client_factory=lambda _: self.client, minimum_update_interval=0)
        self.engine = RoomEngine(self.session)
        self.engine.start()
        self.addCleanup(self.engine.close)
        self.session._playback_thread = mock.Mock()
        self.token = self.engine.acquire("test", command_id="lease-command")["token"]

    def control(self, action, **kwargs):
        revision = self.engine.snapshot()["revision"]
        return self.engine.playback_control(action, token=self.token, expected_revision=revision,
                                            command_id=f"playback-{action}-{revision}", **kwargs)

    def tick(self):
        self.engine._call("_playback_tick")

    def test_play_pause_drag_resume_and_complete(self):
        self.control("play")
        self.assertTrue(self.engine.snapshot()["movement_active"])
        self.tick()
        self.assertEqual(self.engine.snapshot()["roles"]["sta_01"]["position"], [3, 2])
        self.engine.position("sta_01", token=self.token, expected_revision=self.engine.snapshot()["revision"],
                             position=[2, 4], final=True, command_id="manual-drag")
        self.tick()
        state = self.engine.snapshot()
        self.assertEqual(state["roles"]["sta_01"]["position"], [2, 4])
        self.assertEqual(state["roles"]["extender_1"]["position"], [7, 2])
        self.assertEqual(state["playback"]["manual_roles"], ["sta_01"])
        self.control("pause")
        before = self.engine.snapshot()
        writes = len(self.client.applied)
        self.tick()
        after = self.engine.snapshot()
        for field in ("roles", "revision", "daemon", "playback"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(len(self.client.applied), writes)
        self.control("play")
        self.tick()
        state = self.engine.snapshot()
        self.assertEqual(state["playback"]["status"], "completed")
        self.assertEqual(state["roles"]["extender_1"]["position"], [6, 2])
        self.assertEqual(state["roles"]["sta_01"]["position"], [2, 4])
        self.assertFalse(state["movement_active"])
        self.assertEqual(self.store.current()["playback"], state["playback"])

    def test_checkpoints_freeze_after_committed_rf_and_resume_once(self):
        self.session._playback_world["pause_at_ms"] = [1000, 2000]
        for expected_time in (1000, 2000):
            self.control("play")
            self.tick()
            state = self.engine.snapshot()
            self.assertEqual(state["playback"]["status"], "paused")
            self.assertEqual(state["playback"]["checkpoint_ms"], expected_time)
            self.assertEqual(state["roles"]["sta_01"]["position"], [2 + expected_time // 1000, 2])
            self.assertFalse(state["movement_active"])
            writes = len(self.client.applied)
            self.tick()
            self.assertEqual(len(self.client.applied), writes)
            self.assertEqual(self.engine.snapshot()["playback"]["time_ms"], expected_time)
            self.assertEqual(self.store.current()["playback"], state["playback"])
        self.control("play")
        self.tick()
        self.assertEqual(self.engine.snapshot()["playback"]["status"], "completed")
        self.assertIsNone(self.engine.snapshot()["playback"]["checkpoint_ms"])
        self.control("play")
        self.tick()
        self.tick()
        self.assertEqual(self.engine.snapshot()["playback"]["checkpoint_ms"], 1000)

    def test_checkpoint_and_presence_boundaries_are_not_skipped(self):
        self.session._playback_world["pause_at_ms"] = [500, 1250]
        self.session._playback_world["generations"][1]["present"]["sta_01"] = False
        self.control("play")
        self.tick()
        self.assertEqual(self.engine.snapshot()["playback"]["checkpoint_ms"], 500)
        self.control("play")
        self.tick()
        self.assertEqual(self.engine.snapshot()["playback"]["time_ms"], 1000)
        self.assertFalse(self.engine.snapshot()["roles"]["sta_01"]["present"])
        self.tick()
        self.assertEqual(self.engine.snapshot()["playback"]["checkpoint_ms"], 1250)

    def test_invalid_checkpoints_are_rejected_before_rf_writes(self):
        for pauses in (None, True, "1000", [0], [3000], [1000, 1000], [2000, 1000], [True], [1.5]):
            with self.subTest(pauses=pauses):
                self.session._playback_world["pause_at_ms"] = pauses
                writes = len(self.client.applied)
                with self.assertRaises(InteractionError):
                    self.control("play")
                self.assertEqual(len(self.client.applied), writes)

    def test_presence_changes_use_real_rf_and_update_online_expectation(self):
        self.session._playback_world["generations"][1]["present"]["sta_01"] = False
        self.control("play")
        self.tick()
        self.assertEqual(self.engine.snapshot()["expected_online_clients"], 0)
        links = [value for key, value in self.client.values.items() if PLAN["bindings"]["sta_01"]["radio_tx_mac"] in key[:2]]
        self.assertEqual(set(links), {(-20, True)})
        self.tick()
        self.assertEqual(self.engine.snapshot()["expected_online_clients"], 1)

    def test_release_and_expiry_freeze_playback(self):
        self.control("play")
        self.tick()
        self.engine.release(self.token, command_id="release-command")
        paused = self.engine.snapshot()["playback"]
        self.tick()
        self.assertEqual(self.engine.snapshot()["playback"], paused)
        self.assertEqual(paused["status"], "paused")
        self.token = self.engine.acquire("new-owner", command_id="lease-new-owner")["token"]
        self.control("play")
        self.session._lease["expires_monotonic"] = 0
        self.tick()
        self.assertEqual(self.engine.snapshot()["playback"]["status"], "paused")

    def test_invalid_future_frames_are_rejected_before_any_write(self):
        for invalid in ("bounds", "gateway", "missing", "time"):
            with self.subTest(invalid=invalid):
                self.session._playback_world = copy.deepcopy(self.world)
                frame = self.session._playback_world["generations"][-1]
                if invalid == "bounds":
                    frame["positions"]["sta_01"] = [100, 2]
                elif invalid == "gateway":
                    frame["present"]["gateway"] = False
                elif invalid == "missing":
                    del frame["positions"]["sta_01"]
                else:
                    frame["time_ms"] = 0
                writes = len(self.client.applied)
                with self.assertRaises(InteractionError):
                    self.control("play")
                self.assertEqual(len(self.client.applied), writes)
                self.assertEqual(self.engine.snapshot()["playback"]["status"], "paused")

    def test_failed_rf_tick_stops_and_keeps_last_accepted_role(self):
        self.control("play")
        before = self.engine.snapshot()["roles"]
        with mock.patch.object(self.session, "_apply_roles", side_effect=ActuatorError("readback failed")):
            self.tick()
        self.assertEqual(self.engine.snapshot()["roles"], before)
        self.assertEqual(self.engine.snapshot()["playback"]["status"], "paused")

    def test_simultaneous_motion_commits_one_atomic_generation(self):
        self.control("play")
        before = len(self.client.applied)
        epoch = self.session._environment_epoch
        self.tick()
        self.assertEqual(len(self.client.applied) - before, 1)
        self.assertEqual(self.session._environment_epoch - epoch, 1)
        commits = [event for event in self.store.all() if event["kind"] == "room.position.committed"]
        self.assertEqual(commits, [])
        frame = self.store.current()["latest"]["interaction.playback.progress"]["payload"]
        self.assertEqual(frame["playback"]["time_ms"], 1000)
        self.assertEqual(frame["roles"]["sta_01"]["position"], [3, 2])
        self.assertEqual(frame["roles"]["extender_1"]["position"], [8, 2])
        self.assertEqual(frame["daemon_generation"], self.client.generation)

    def test_every_published_playback_projection_has_matching_clock_and_pose(self):
        self.control("play")
        observed = []
        publish = self.store.emit
        def observe(*args, **kwargs):
            event = publish(*args, **kwargs)
            state = self.store.current()
            observed.append((state["playback"]["time_ms"],
                             state["roles"]["sta_01"]["authoritative_position"]))
            return event
        with mock.patch.object(self.store, "emit", side_effect=observe):
            self.tick()
        self.assertTrue(observed)
        self.assertTrue(all(position == [2 + elapsed // 1000, 2]
                            for elapsed, position in observed), observed)

    def test_authorization_revision_and_idempotency(self):
        with self.assertRaises(InteractionError):
            self.engine.playback_control("play", token="wrong", expected_revision=0, command_id="wrong-token")
        first = self.engine.playback_control("play", token=self.token, expected_revision=0, command_id="same-command")
        repeated = self.engine.playback_control("play", token=self.token, expected_revision=0, command_id="same-command")
        self.assertEqual(first, repeated)
        with self.assertRaises(InteractionError):
            self.engine.playback_control("pause", token=self.token, expected_revision=0, command_id="stale-command")

    def test_presence_control_runs_even_when_rf_values_do_not_change(self):
        self.session._roles["sta_01"]["present"] = False
        updates, links = self.session._links_for_role("sta_01")
        for item in updates:
            key = (item["source"], item["destination"], item["frequency_mhz"])
            value = (item["value"], item["override"])
            self.session._applied_values[key] = value
            self.client.values[key] = value
        self.session._roles["sta_01"]["present"] = True
        self.session.disconnect_client = mock.Mock(return_value=nullcontext())
        self.session.reconnect_client = mock.Mock()
        before = len(self.client.applied)
        with mock.patch.object(self.session, "_links_for_role", return_value=(updates, links)):
            for present in (False, True):
                self.engine.presence("sta_01", present=present, token=self.token,
                    expected_revision=self.engine.snapshot()["revision"],
                    command_id=f"presence-noop-{present}")
        self.session.disconnect_client.assert_called_once_with("sta_01")
        self.session.reconnect_client.assert_called_once_with("sta_01")
        self.assertEqual(len(self.client.applied), before)

    def test_default_worker_stops_cleanly(self):
        self.session._playback_thread = None
        self.control("play")
        thread = self.session._playback_thread
        self.assertTrue(thread.is_alive())
        self.assertTrue(self.engine.close())
        self.assertFalse(thread.is_alive())

    def test_play_after_completion_reapplies_initial_frame(self):
        self.control("play")
        for step in range(3):
            self.tick()
        self.control("play")
        self.tick()
        state = self.engine.snapshot()
        self.assertEqual(state["playback"]["time_ms"], 0)
        self.assertEqual(state["roles"]["sta_01"]["position"], [2, 2])
        self.tick()
        self.assertEqual(self.engine.snapshot()["playback"]["time_ms"], 1000)

    def test_subsecond_geometry_is_sampled_without_skipping_presence_edges(self):
        frames = self.session._playback_world["generations"]
        frames[1]["time_ms"] = 200
        frames[2]["time_ms"] = 400
        frames[3]["time_ms"] = 1500
        frames[3]["present"]["sta_01"] = False
        self.control("play")
        self.tick()
        self.assertEqual(self.engine.snapshot()["playback"]["time_ms"], 1000)
        self.tick()
        state = self.engine.snapshot()
        self.assertEqual(state["playback"]["time_ms"], 1500)
        self.assertFalse(state["roles"]["sta_01"]["present"])
