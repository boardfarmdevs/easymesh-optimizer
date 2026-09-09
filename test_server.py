from __future__ import annotations

import json
import socket
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from room_demo.events import EventStore
from room_demo.server import RoomDemoServer


class FakeInteractions:
    def __init__(self):
        self.revision = 2

    def snapshot(self):
        return {"enabled": True, "revision": self.revision}

    def acquire(self, owner, **_body):
        return {"token": "lease-token", "owner": owner, "revision": self.revision}

    def renew(self, token, **_body):
        return {"token_seen": token, "revision": self.revision}

    def release(self, token, **_body):
        return {"released": token == "lease-token", "revision": self.revision}

    def position(self, role, **body):
        self.revision += 1
        return {"role": role, "revision": self.revision, **body}

    def presence(self, role, **body):
        self.revision += 1
        return {"role": role, "revision": self.revision, **body}

    def move(self, role, **body):
        self.revision += 1
        return {
            "revision": self.revision,
            "movement": {"id": "move-1", "role": role, "status": "running"},
            **body,
        }

    def movement_control(self, movement_id, action, **body):
        self.revision += 1
        return {
            "revision": self.revision,
            "movement": {"id": movement_id, "status": action},
            **body,
        }

    def playback_control(self, action, **body):
        self.revision += 1
        return {"revision": self.revision, "playback": {"status": action}, **body}

    def select_traffic_probe(self, role, **body):
        self.revision += 1
        return {"revision": self.revision, "traffic_probe": {"role": role}, **body}

    def start_recording(self, **body):
        return {"revision": self.revision, "recording": {"active": True}, **body}

    def stop_recording(self, **body):
        return {
            "revision": self.revision,
            "recording": {"active": False, "export_ready": True},
            **body,
        }

    def recorded_world(self):
        return {"schema": "wmdcfg.world-plan.v1", "name": "recorded"}

    def world_catalog(self):
        return {"enabled": True, "clients": 20, "worlds": []}

    def apply_world(self, selection, **body):
        self.revision += 1
        return {"selection": selection, "revision": self.revision, **body}


class ServerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "index.html").write_text("<html>viewer</html>")
        (root / "vendor").mkdir()
        (root / "vendor" / "three.min.js").write_text("window.THREE={};")
        (root / "interaction-model.js").write_text("window.RoomInteractionModel={};")
        world = {
            "schema": "wmdcfg.world-plan.v1",
            "name": "test-world",
            "duration_ms": 1000,
            "tick_ms": 100,
        }
        self.store = EventStore("run-1", world, root / "events.jsonl")
        self.server = RoomDemoServer(("127.0.0.1", 0), self.store, root)
        self.server.start()
        self.addCleanup(self.server.close)
        self.base = f"http://127.0.0.1:{self.server.address[1]}"

    def _json(self, path):
        with urllib.request.urlopen(self.base + path, timeout=2) as response:
            return json.load(response)

    def _request(self, path, method, body):
        request = urllib.request.Request(
            self.base + path,
            method=method,
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.load(response)

    def test_health_current_world_and_viewer(self):
        self.assertEqual(self._json("/healthz")["status"], "ok")
        self.assertEqual(self._json("/api/demo/current")["state"], "preparing")
        self.assertEqual(self._json("/api/demo/world")["name"], "test-world")
        with urllib.request.urlopen(self.base + "/viewer/?mode=live", timeout=2) as response:
            self.assertIn(b"viewer", response.read())

    def test_viewer_defaults_are_host_specific_without_changing_static_files(self):
        viewer_root = Path(__file__).resolve().parents[3] / "gen/wmediumd/configurator/worlds/viewer"
        original = (viewer_root / "index.html").read_bytes()
        self.assertIn(b'<meta name="room-viewer-mode" content="no-connect">', original)
        for interactions, replay, expected in [
            (None, False, "live"),
            (FakeInteractions(), False, "interactive"),
            (None, True, "replay"),
        ]:
            with self.subTest(mode=expected):
                server = RoomDemoServer(("127.0.0.1", 0), self.store, viewer_root, interactions, replay=replay)
                server.start()
                try:
                    base = f"http://127.0.0.1:{server.address[1]}"
                    for route in ["/", "/viewer", "/viewer/", "/viewer/index.html", "/?mode=live&world=example"]:
                        with urllib.request.urlopen(base + route, timeout=2) as response:
                            body = response.read()
                            self.assertIn(f'<meta name="room-viewer-mode" content="{expected}">'.encode(), body)
                            self.assertEqual(response.headers["Cache-Control"], "no-store")
                            self.assertEqual(int(response.headers["Content-Length"]), len(body))
                            if route == "/":
                                self.assertEqual(response.url, base + "/viewer/")
                            elif route.startswith("/?"):
                                self.assertEqual(response.url, base + "/viewer/?mode=live&world=example")
                    with urllib.request.urlopen(base + "/viewer/interaction-model.js", timeout=2) as response:
                        self.assertEqual(response.read(), (viewer_root / "interaction-model.js").read_bytes())
                    self.assertEqual((viewer_root / "index.html").read_bytes(), original)
                finally:
                    server.close()

    def test_event_stream_limit_reserves_regular_api_capacity(self):
        for _ in range(16):
            self.assertTrue(self.server.httpd.event_streams.acquire(blocking=False))
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(self.base + "/api/demo/events", timeout=2)
            self.assertEqual(caught.exception.code, 503)
            self.assertEqual(self._json("/healthz")["status"], "ok")
        finally:
            for _ in range(16):
                self.server.httpd.event_streams.release()

    def test_connection_capacity_rejects_without_creating_a_worker(self):
        for _ in range(64):
            self.assertTrue(self.server.httpd.connections.acquire(blocking=False))
        try:
            with socket.create_connection(self.server.address, timeout=2) as connection:
                self.assertIn(b"503", connection.recv(1024))
        finally:
            for _ in range(64):
                self.server.httpd.connections.release()
        self.assertEqual(self._json("/healthz")["status"], "ok")
        with urllib.request.urlopen(
            self.base + "/viewer/vendor/three.min.js", timeout=2
        ) as response:
            self.assertIn(b"THREE", response.read())
        with urllib.request.urlopen(
            self.base + "/viewer/interaction-model.js", timeout=2
        ) as response:
            self.assertIn(b"RoomInteractionModel", response.read())

    def test_post_is_not_a_control_surface(self):
        request = urllib.request.Request(self.base + "/api/demo/current", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 405)

    def test_expired_sse_history_requests_explicit_state_resynchronization(self):
        self.store._history_events = 1
        self.store.emit("scenario.clock", 0, {})
        self.store.emit("scenario.clock", 0, {})
        with urllib.request.urlopen(self.base + "/api/demo/events?after=0", timeout=2) as response:
            lines = [response.readline().decode().strip() for _index in range(4)]
        self.assertEqual(lines[0], "id: 2")
        self.assertEqual(lines[1], "event: reset")
        self.assertIn("history_expired", lines[2])
        self.assertTrue(self._json("/api/demo/storage")["history"]["truncated"])

    def test_sse_replays_an_ordered_event(self):
        self.store.publish({
            "schema": "easymesh.room-demo.event.v1",
            "run_id": "run-1",
            "sequence": 1,
            "recorded_at": "2026-09-03T00:00:00+00:00",
            "world_time_ms": 100,
            "kind": "scenario.clock",
            "payload": {},
        })
        with urllib.request.urlopen(self.base + "/api/demo/events?after=0", timeout=2) as response:
            self.assertEqual(response.readline().decode().strip(), "id: 1")
            data = response.readline().decode().strip()
        self.assertTrue(data.startswith("data: "))
        self.assertEqual(json.loads(data[6:])["kind"], "scenario.clock")

    def test_events_json_supports_browser_replay(self):
        self.store.emit("scenario.clock", 100, producer="wmdcfg")
        payload = self._json("/api/demo/events.json")
        self.assertEqual(payload["schema"], "easymesh.room-demo.events.v1")
        self.assertEqual(payload["events"][0]["kind"], "scenario.clock")


class InteractiveServerTests(unittest.TestCase):
    def test_traffic_probe_route_requires_json_and_revision_without_operator(self):
        status, result = self._request("/api/demo/traffic-probe", "POST", {
            "role": "sta_01", "token": "lease-token", "expected_revision": 2,
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["traffic_probe"]["role"], "sta_01")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._request("/api/demo/traffic-probe", "POST", {"role": "sta_01"})
        self.assertEqual(caught.exception.code, 428)
        request = urllib.request.Request(self.base + "/api/demo/traffic-probe", method="POST", data=b'{}')
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 415)

    def test_playback_uses_revisioned_endpoint_without_operator(self):
        status, result = self._request("/api/demo/playback", "POST", {
            "action": "play", "token": "lease-token", "expected_revision": 2,
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["playback"]["status"], "play")

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "index.html").write_text("<html>viewer</html>")
        world = {"name": "world", "duration_ms": 1000, "tick_ms": 100}
        store = EventStore("run", world, root / "events.jsonl")
        self.interactions = FakeInteractions()
        self.server = RoomDemoServer(
            ("127.0.0.1", 0), store, root, self.interactions,
        )
        self.server.start()
        self.addCleanup(self.server.close)
        self.base = f"http://127.0.0.1:{self.server.address[1]}"

    def _request(self, path, method="GET", body=None):
        if body is not None and method != "GET":
            body = dict(body)
            body.setdefault("command_id", f"test-command-{id(body)}")
        data = None if body is None else json.dumps(body).encode()
        headers = {
            "Content-Type": "application/json",
        }
        if body is not None and "expected_revision" in body:
            headers["If-Match"] = (
                f'"world-revision-{body["expected_revision"]}"'
            )
        request = urllib.request.Request(
            self.base + path, method=method, data=data,
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.load(response)

    def test_world_apply_requires_revision_without_operator(self):
        _, catalog = self._request("/api/demo/worlds")
        self.assertEqual(catalog["clients"], 20)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._request("/api/demo/world/apply", "POST", {"world": "default"})
        self.assertEqual(caught.exception.code, 428)
        _, applied = self._request("/api/demo/world/apply", "POST", {
            "world": "home-a-border-hover", "expected_revision": 2, "token": "lease-token"})
        self.assertEqual(applied["selection"], "home-a-border-hover")
        request = urllib.request.Request(self.base + "/api/demo/world/apply", method="POST",
            data=b'{"world":"default"}', headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 428)

    def test_lease_position_presence_and_release_routes(self):
        _, state = self._request("/api/demo/interactions")
        self.assertEqual(state["revision"], 2)
        status, lease = self._request(
            "/api/demo/interactions/lease", "POST", {"owner": "browser"}
        )
        self.assertEqual(status, 201)
        _, moved = self._request(
            "/api/demo/roles/sta_01/position", "PUT",
            {"token": lease["token"], "expected_revision": 2,
             "position": [4, 2], "final": True},
        )
        self.assertEqual(moved["role"], "sta_01")
        self.assertEqual(moved["position"], [4, 2])
        _, presence = self._request(
            "/api/demo/roles/sta_01/presence", "PUT",
            {"token": lease["token"], "expected_revision": 3,
             "present": False},
        )
        self.assertFalse(presence["present"])
        _, released = self._request(
            "/api/demo/interactions/lease", "DELETE", {"token": lease["token"]}
        )
        self.assertTrue(released["released"])

    def test_server_owned_movement_routes(self):
        _, lease = self._request(
            "/api/demo/interactions/lease", "POST", {"owner": "browser"}
        )
        status, started = self._request(
            "/api/demo/roles/sta_01/move", "POST",
            {"token": lease["token"], "expected_revision": 2,
             "destination": [8, 2], "speed_mps": 1.4},
        )
        self.assertEqual(status, 201)
        self.assertEqual(started["movement"]["role"], "sta_01")
        _, paused = self._request(
            "/api/demo/movements/move-1/pause", "POST",
            {"token": lease["token"], "expected_revision": 3},
        )
        self.assertEqual(paused["movement"]["status"], "pause")
        _, cancelled = self._request(
            "/api/demo/movements/move-1", "DELETE",
            {"token": lease["token"], "expected_revision": 4},
        )
        self.assertEqual(cancelled["movement"]["status"], "cancel")

    def test_recording_routes(self):
        _, lease = self._request(
            "/api/demo/interactions/lease", "POST", {"owner": "browser"}
        )
        status, started = self._request(
            "/api/demo/recording/start", "POST",
            {"token": lease["token"], "expected_revision": 2, "name": "walk"},
        )
        self.assertEqual(status, 201)
        self.assertTrue(started["recording"]["active"])
        _, stopped = self._request(
            "/api/demo/recording/stop", "POST",
            {"token": lease["token"], "expected_revision": 2},
        )
        self.assertTrue(stopped["recording"]["export_ready"])
        _, world = self._request("/api/demo/recording/world")
        self.assertEqual(world["schema"], "wmdcfg.world-plan.v1")

    def test_world_mutation_requires_if_match(self):
        request = urllib.request.Request(
            self.base + "/api/demo/roles/sta_01/position",
            method="PUT",
            data=json.dumps({
                "token": "lease-token",
                "expected_revision": 2,
                "position": [4, 2],
                "command_id": "test-no-if-match",
            }).encode(),
            headers={
                "Content-Type": "application/json",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 428)

    def test_cross_origin_write_is_rejected(self):
        request = urllib.request.Request(
            self.base + "/api/demo/interactions/lease",
            method="POST",
            data=json.dumps({
                "owner": "browser",
                "command_id": "test-cross-origin",
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": "http://not-the-room.example",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 403)

    def test_lease_without_operator_capability_is_accepted(self):
        request = urllib.request.Request(
            self.base + "/api/demo/interactions/lease",
            method="POST",
            data=json.dumps({
                "owner": "browser",
                "command_id": "test-no-operator",
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            self.assertEqual(response.status, 201)
            self.assertEqual(json.load(response)["token"], "lease-token")

    def test_cross_origin_put_and_delete_remain_rejected_without_operator(self):
        for method, path in (("PUT", "/api/demo/roles/sta_01/position"),
                             ("DELETE", "/api/demo/interactions/lease")):
            with self.subTest(method=method):
                request = urllib.request.Request(self.base + path, method=method,
                    data=b'{}', headers={"Content-Type": "application/json",
                                         "Origin": "http://not-the-room.example"})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=2)
                self.assertEqual(caught.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
