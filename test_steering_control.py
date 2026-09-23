from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
import urllib.request

from room_demo.engine import RoomEngine
from room_demo.events import EventStore
from room_demo.interactions import InteractiveMediumSession
from room_demo.server import RoomDemoServer
from room_demo.steering_safety import SteeringSafety
from test_interactions import FakeClient, WORLD, LAYOUT, PLAN


class SteeringControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.now = 0
        self.safety = SteeringSafety(clock=lambda: self.now)
        self.safety.sync(1, {"station": (1,)})
        self.store = EventStore("safety-test", WORLD, root / "events.jsonl")
        self.client = FakeClient("fake")
        self.session = InteractiveMediumSession(
            self.store, WORLD, LAYOUT, PLAN, "fake",
            client_factory=lambda _path: self.client, minimum_update_interval=0,
            resume_steering=self.safety.resume)
        self.engine = RoomEngine(self.session)
        self.engine.start()
        self.addCleanup(self.engine.close)
        self.server = RoomDemoServer(("127.0.0.1", 0), self.store, root, self.engine,
                                     optimizer_status=self.safety.snapshot)
        self.server.start()
        self.addCleanup(self.server.close)
        self.base = f"http://127.0.0.1:{self.server.address[1]}"
        self.lease = self.engine.acquire("safety-test", command_id="lease-command-1")

    def pause(self):
        for attempt in range(3):
            self.now += 5
            ticket, reason = self.safety.reserve("station", "source", "target")
            self.assertIsNone(reason)
            self.safety.complete(ticket, False, "association_timeout")
        self.assertEqual(self.safety.snapshot()["state"], "paused")

    def request(self, body=None, headers=None):
        options = {"Content-Type": "application/json", **(headers or {})}
        if body is not None:
            options.setdefault("If-Match", f'"world-revision-{body["expected_revision"]}"')
        request = urllib.request.Request(
            self.base + ("/api/demo/optimizer/resume" if body is not None else "/api/demo/optimizer/safety"),
            data=json.dumps(body).encode() if body is not None else None, headers=options)
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)

    def body(self, **changes):
        return {"token": self.lease["token"], "command_id": "resume-command-1",
                "expected_revision": self.session.snapshot()["revision"],
                "expected_pause_revision": self.safety.snapshot()["pause_revision"], **changes}

    def test_resume_keeps_room_rf_playback_and_counters(self):
        self.pause()
        before = self.session.snapshot()
        applied = len(self.client.applied)
        status = self.request()
        self.assertEqual(status, self.safety.snapshot())
        self.assertEqual(self.request(self.body())["steering_safety"]["paused_clients"], [])
        after = self.session.snapshot()
        for field in ("revision", "environment_epoch", "candidate_epochs", "roles",
                      "playback", "traffic_probe", "medium_generation"):
            self.assertEqual(before.get(field), after.get(field), field)
        self.assertEqual(len(self.client.applied), applied)
        status = self.request()
        self.assertEqual(status["total_requests"], 3)
        self.assertEqual(status["requests_in_window"], 3)
        self.assertEqual(self.safety.check("station", "source", "target"), "steering_client_cooldown")

    def test_command_retry_does_not_rearm_a_new_pause(self):
        self.pause()
        body = self.body()
        original = self.request(body)
        self.pause()
        self.assertEqual(self.request(body), original)
        self.assertEqual(self.safety.snapshot()["state"], "paused")

    def test_invalid_controls_cannot_resume(self):
        self.pause()
        cases = [
            (self.body(token="wrong"), {}, 403),
            (self.body(expected_revision=999), {}, 409),
            (self.body(expected_pause_revision=999), {}, 409),
            (self.body(expected_pause_revision=None), {}, 400),
            (self.body(command_id=""), {}, 400),
            (self.body(), {"Origin": "https://unrelated.invalid"}, 403),
        ]
        for body, headers, expected in cases:
            with self.subTest(body=body, headers=headers):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.request(body, headers)
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(self.safety.snapshot()["state"], "paused")

    def test_missing_control_callback_is_not_supported(self):
        self.pause()
        self.session._resume_steering = None
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(self.body())
        self.assertEqual(caught.exception.code, 409)
        self.assertEqual(self.safety.snapshot()["state"], "paused")


if __name__ == "__main__":
    unittest.main()
