from __future__ import annotations

import copy
from contextlib import contextmanager
import json
import tempfile
import threading
from pathlib import Path
import unittest
from unittest import mock

from room_demo.engine import RoomEngine
from room_demo.events import EventStore
from room_demo.interactions import InteractionError, InteractiveMediumSession
from room_demo.recovery import RecoveryJournal, load_recovery, recover_medium
from room_demo.worlds import BoundWorlds
from wmdcfg.actuator import ActuatorError
from wmdcfg.world import _hash, load_json

from test_interactions import FakeClient


ROOT = Path(__file__).resolve().parents[2] / "wmediumd/configurator/worlds"


def signed(world):
    result = copy.deepcopy(world)
    result.pop("golden_sha256", None)
    result["golden_sha256"] = _hash(result)
    return result


class WorldSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.world = load_json(ROOT / "golden/home-a-private-client-room-walk.world.json")
        self.layout = load_json(ROOT / "layouts/home-five-agent.json")
        self.worlds = BoundWorlds(self.world, self.layout, ROOT)
        bindings = {}
        for index, (role, kind) in enumerate(sorted(self.world["roles"].items()), 1):
            bindings[role] = {"role_type": kind, "radio_tx_mac": f"02:00:00:{index:02x}:00:00"}
            if kind == "fronthaul_ap":
                bindings[role]["fronthaul_frequencies_mhz"] = {"2.4": 2412, "5": 5180, "6": 5955}
                bindings[role]["band_radios"] = {
                    band: {"tx_mac": f"02:00:00:{index:02x}:00:{ordinal:02x}"}
                    for ordinal, band in enumerate(("2.4", "5", "6"), 1)
                }
        self.plan = {"bindings": bindings}
        self.client = FakeClient("unused")
        self.store = EventStore("switch-test", self.world, self.directory / "live-events.jsonl")
        self.recovery = self.directory / "recovery.json"
        self.session = InteractiveMediumSession(
            self.store, self.world, self.layout, self.plan, "unused",
            client_factory=lambda _: self.client, worlds=self.worlds,
            recovery=RecoveryJournal(self.recovery, "switch-test", "inventory"),
        )
        self.engine = RoomEngine(self.session)
        self.engine.start()
        self.addCleanup(self.engine.close)
        self.token = self.engine.acquire("test", command_id="lease-command")["token"]

    def test_adaptive_worlds_apply_mesh_geometry_and_report_verified_snr(self):
        self.engine.close()
        self.session = InteractiveMediumSession(
            self.store, self.world, self.layout, self.plan, "unused",
            client_factory=lambda _: self.client, worlds=self.worlds, adaptive_backhaul=True,
        )
        self.engine = RoomEngine(self.session)
        self.engine.start()
        self.addCleanup(self.engine.close)
        self.token = self.engine.acquire("adaptive-test", command_id="adaptive-lease")["token"]
        initial = self.engine.snapshot()["backhaul_links"]
        self.assertEqual(len(initial), 60)
        self.assertEqual(self.session._protected_backhaul, {})
        self.assertEqual(self.engine.snapshot()["backhaul_policy"], "adaptive-rdk")
        self.engine.position("extender_1", position=[10, 6], final=True,
                             token=self.token, expected_revision=0, command_id="mesh-move")
        moved = self.engine.snapshot()["backhaul_links"]
        self.assertNotEqual(initial, moved)
        self.engine.presence("extender_1", present=False, token=self.token,
                             expected_revision=1, command_id="fronthaul-off")
        self.assertEqual(self.engine.snapshot()["backhaul_links"], moved)
        epoch = self.engine.snapshot()["environment_epoch"]
        with self.assertRaises(InteractionError):
            self.engine.backhaul_action(lambda: self.fail("stale action ran"), expected_epoch=epoch - 1)
        self.assertEqual(self.engine.backhaul_action(lambda: "verified", expected_epoch=epoch), "verified")
        self.assertGreater(self.engine.snapshot()["environment_epoch"], epoch)
        for name in ("home-a-border-hover", "home-a-stationary", "home-b-slow-walk-ten", "default"):
            self.apply(name)
            snapshot = self.engine.snapshot()
            self.assertEqual(snapshot["backhaul_policy"], "adaptive-rdk")
            self.assertEqual(len(snapshot["backhaul_links"]), 60)
            for link in snapshot["backhaul_links"]:
                source = self.plan["bindings"][link["source_role"]]["band_radios"][link["band"]]["tx_mac"]
                destination = self.plan["bindings"][link["destination_role"]]["band_radios"][link["band"]]["tx_mac"]
                self.assertEqual(self.client.values[(source, destination, link["frequency_mhz"])], (link["snr_db"], True))
        self.assertEqual(self.engine.snapshot()["backhaul_links"], initial)

    def apply(self, world, command_id=None, revision=None):
        current = self.engine.snapshot()["revision"]
        return self.engine.apply_world(
            world, token=self.token, expected_revision=current if revision is None else revision,
            command_id=command_id or f"world-command-{current}",
        )

    def test_catalog_and_multiple_worlds_use_fixed_pool_and_restore(self):
        self.assertEqual(len(self.worlds.catalog()["worlds"]), 14)
        bindings = copy.deepcopy(self.plan)
        backhaul = dict(self.session._protected_backhaul)
        self.assertEqual(len(backhaul), 60)
        for name, online in (("home-a-border-hover", 12), ("home-a-stationary", 10),
                             ("home-b-slow-walk-ten", 20), ("home-a-one-client-handover", 11),
                             ("large-room-perimeter-counter-roam", 12),
                             ("large-room-extender-evacuation", 12), ("default", 20)):
            result = self.apply(name)
            self.assertEqual(result["expected_online_clients"], online)
            snapshot = self.engine.snapshot()
            self.assertEqual(snapshot["pool_clients"], 20)
            self.assertEqual(len(snapshot["roles"]), 25)
            self.assertEqual(self.plan, bindings)
            self.assertTrue(all(self.client.values[key] == value for key, value in backhaul.items()))
            self.assertEqual(self.store.current_world()["counts"]["stations"], online)
            for role in self.session._allowed_roles:
                if snapshot["roles"][role]["present"]:
                    continue
                mac = bindings["bindings"][role]["radio_tx_mac"]
                isolated = [value for key, value in self.client.values.items() if mac in key[:2]]
                self.assertEqual(len(isolated), 30)
                self.assertEqual(set(isolated), {(-20, True)})
        self.assertTrue(self.engine.close())
        self.assertFalse(self.client.values)
        self.assertEqual(load_recovery(self.recovery)["state"], "restored")

    def test_perimeter_checkpoints_commit_rf_and_preserve_the_fixed_pool(self):
        self.session._playback_thread = mock.Mock()
        self.apply("large-room-perimeter-counter-roam")
        for expected_time in (14000, 28000, 42000, 60000):
            revision = self.engine.snapshot()["revision"]
            self.engine.playback_control("play", token=self.token, expected_revision=revision,
                                         command_id=f"perimeter-play-{expected_time}")
            for step in range(20):
                self.engine._call("_playback_tick")
                state = self.engine.snapshot()
                if state["playback"]["status"] != "playing":
                    break
            self.assertEqual(state["playback"]["time_ms"], expected_time)
            self.assertEqual(state["expected_online_clients"], 12)
            self.assertEqual(state["pool_clients"], 20)
            self.assertEqual(len(state["roles"]), 25)
            self.assertFalse(state["movement_active"])
            checkpoint = expected_time if expected_time < 60000 else None
            self.assertEqual(state["playback"]["checkpoint_ms"], checkpoint)
        self.apply("default")
        self.assertIsNone(self.engine.snapshot()["playback"]["checkpoint_ms"])

    def test_switch_stops_playback_clears_pins_and_retains_backhaul(self):
        self.session._playback_thread = mock.Mock()
        self.engine.playback_control("play", token=self.token, expected_revision=0, command_id="start-playback")
        self.engine.position("sta_static_03", token=self.token, expected_revision=1, position=[2, 3],
                             final=True, command_id="pin-during-playback")
        self.engine._call("_playback_tick")
        self.apply("home-a-band-walk-small")
        state = self.engine.snapshot()
        self.assertEqual(state["playback"]["status"], "paused")
        self.assertEqual(state["playback"]["time_ms"], 0)
        self.assertEqual(state["playback"]["manual_roles"], [])
        self.assertGreater(len(self.session._playback_world["generations"]), 1)
        self.assertEqual(len(self.store.current_world()["generations"]), 1)
        self.assertTrue(all(self.client.values[key] == value for key, value in self.session._protected_backhaul.items()))

    def test_upload_validates_hash_layout_roles_and_presence_before_any_write(self):
        world, _ = self.worlds.select("home-a-border-hover")
        invalid = []
        changed = copy.deepcopy(world)
        del changed["name"]
        invalid.append(signed(changed))
        changed = copy.deepcopy(world)
        changed["generations"][0]["links"] = []
        invalid.append(signed(changed))
        changed = copy.deepcopy(world)
        changed["roles"]["new_client"] = "station"
        invalid.append(signed(changed))
        changed = copy.deepcopy(world)
        del changed["roles"]["extender_1"]
        invalid.append(signed(changed))
        changed = copy.deepcopy(world)
        changed["generations"][0]["present"]["gateway"] = False
        invalid.append(signed(changed))
        changed = copy.deepcopy(world)
        changed["generations"][0]["positions"]["sta_mobile_01"] = [999, 0]
        invalid.append(signed(changed))
        changed = copy.deepcopy(world)
        changed["layout_sha256"] = "incorrect"
        invalid.append(signed(changed))
        invalid.extend(["../../bad", "missing-world", {"schema": "wmdcfg.world-plan.v1"}])
        before = copy.deepcopy(self.client.values)
        generation = self.client.generation
        for selection in invalid:
            with self.assertRaises(InteractionError) as caught:
                self.apply(selection)
            self.assertEqual(caught.exception.code, "unsupported_world")
            self.assertEqual(self.client.values, before)
            self.assertEqual(self.client.generation, generation)
        self.assertEqual(self.apply(world)["expected_online_clients"], 12)

    def test_offline_roles_cannot_be_reenabled_or_steered_until_world_restored(self):
        self.apply("home-a-border-hover")
        with self.assertRaises(InteractionError):
            self.engine.presence("sta_mobile_10", token=self.token,
                                 expected_revision=1, present=True, command_id="hidden-presence")
        with self.assertRaises(InteractionError):
            self.engine.steering_action("sta_mobile_10", "gateway", "extender_1", "5", lambda: None)
        with self.assertRaises(InteractionError):
            self.engine.steering_action("sta_mobile_01", "gateway", "extender_1", "5", lambda: None, expected_epoch=0)
        self.apply("default")
        self.assertIn("sta_mobile_10", self.engine.snapshot()["presence_roles"])

    def test_parallel_disconnects_precede_one_atomic_world_isolation(self):
        disconnected = []
        barrier = threading.Barrier(4)
        baseline_generation = self.client.generation

        @contextmanager
        def disconnect(role):
            mac = self.plan["bindings"][role]["radio_tx_mac"]
            self.assertTrue(any(value[0] > -20 for key, value in self.client.values.items() if mac in key[:2]))
            disconnected.append(role)
            barrier.wait(timeout=2)
            yield
            self.assertEqual({value for key, value in self.client.values.items()
                              if mac in key[:2]}, {(-20, True)})

        self.session.disconnect_client = disconnect
        self.apply("home-a-border-hover")
        self.assertEqual(len(disconnected), 8)
        self.assertEqual(self.client.generation, baseline_generation + 1)
        self.apply("default")
        self.assertEqual(len(disconnected), 8)

    def test_modeled_backhaul_does_not_create_an_external_parent_manager(self):
        self.engine.close()
        self.session = InteractiveMediumSession(
            self.store, self.world, self.layout, self.plan, "unused",
            client_factory=lambda _: self.client, worlds=self.worlds, model_backhaul=True,
        )
        self.engine = RoomEngine(self.session)
        self.engine.start()
        self.addCleanup(self.engine.close)
        self.token = self.engine.acquire("modeled", command_id="modeled-lease")["token"]
        self.assertEqual(self.engine.snapshot()["backhaul_policy"], "modeled")
        self.assertFalse(self.session.adaptive_backhaul)
        self.assertEqual(self.session._protected_backhaul, {})
        initial = self.engine.snapshot()["backhaul_links"]
        self.engine.position("extender_1", position=[10, 6], final=True,
                             token=self.token, expected_revision=0, command_id="modeled-move")
        self.assertNotEqual(initial, self.engine.snapshot()["backhaul_links"])

    def test_partial_disconnect_failure_rolls_back_all_applied_isolation(self):
        before = copy.deepcopy(self.client.values)
        calls = []

        @contextmanager
        def disconnect(role):
            calls.append(role)
            if len(calls) == 2:
                raise RuntimeError("client disconnect failed")
            yield

        self.session.disconnect_client = disconnect
        with self.assertRaises(RuntimeError):
            self.apply("home-a-border-hover")
        self.assertEqual(self.client.values, before)
        self.assertEqual(self.engine.snapshot()["expected_online_clients"], 20)

    def test_revision_lease_idempotency_and_recording_guards(self):
        result = self.apply("home-a-border-hover", command_id="retry-world")
        generation = self.client.generation
        self.assertEqual(self.apply("home-a-border-hover", command_id="retry-world", revision=0), result)
        self.assertEqual(self.client.generation, generation)
        with self.assertRaises(InteractionError):
            self.apply("default", revision=0)
        self.engine.start_recording(token=self.token, expected_revision=1, command_id="record-command")
        with self.assertRaises(InteractionError) as caught:
            self.apply("default")
        self.assertEqual(caught.exception.code, "recording_active")
        self.engine.stop_recording(token=self.token, expected_revision=1, command_id="record-stop")
        recording = self.engine.recorded_world()
        self.assertEqual(recording["counts"]["stations"], 12)
        self.assertEqual(len(recording["roles"]), 17)
        self.assertEqual(self.worlds.select(recording)[0]["counts"]["stations"], 12)
        self.engine.release(self.token, command_id="lease-release")
        with self.assertRaises(InteractionError):
            self.apply("default")

    def test_failed_readback_rolls_back_previous_room(self):
        original = self.client.get_frequency_link
        before = copy.deepcopy(self.client.values)
        failed = False

        def readback(*args):
            nonlocal failed
            generation, value, overridden = original(*args)
            if not failed:
                failed = True
                return generation, value + 1, overridden
            return generation, value, overridden

        self.client.get_frequency_link = readback
        with self.assertRaises(ActuatorError):
            self.apply("home-a-border-hover")
        self.assertEqual(self.client.values, before)
        self.assertEqual(self.engine.snapshot()["revision"], 0)
        self.assertEqual(self.store.current_world()["name"], self.world["name"])

    def test_crash_recovery_and_event_replay_preserve_original_baseline(self):
        self.apply("home-a-border-hover")
        self.apply("home-b-slow-walk-ten")
        (self.directory / "world.json").write_text(json.dumps(self.world))
        replay = EventStore.from_evidence(self.directory)
        self.assertEqual(replay.current(), self.store.current())
        self.assertEqual(replay.current_world()["name"], "home-five-agent-shifted--slow-walk-ten")
        self.assertEqual(replay.initial_world["name"], self.world["name"])
        result = recover_medium(self.recovery, "unused", client_factory=lambda _: self.client)
        self.assertEqual(result["status"], "restored")
        self.assertFalse(self.client.values)
        self.session._generation = self.client.generation


if __name__ == "__main__":
    unittest.main()
