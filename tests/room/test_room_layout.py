import datetime as dt
from pathlib import Path
import unittest

from room_service.events import EventStore


class RoomLayoutEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.world = {"name": "test", "duration_ms": 1000, "tick_ms": 100, "roles": {}}
        self.store = EventStore("run", self.world, Path("/unused"), persist=False)
        self.decision = {"action": "steer", "sta_mac": "02:00:00:10:01:00",
                         "source_bssid": "02:00:00:00:01:00", "target_bssid": "02:00:00:00:04:00"}

    def action(self, identity, phase, success=True, **overrides):
        return self.store.emit("optimizer.action", 0, {
            "world_epoch": 0, "action_id": identity, "phase": phase,
            "decision": self.decision, "result": {"success": success}, **overrides,
        })

    def test_only_matching_accepted_requests_have_btm_evidence(self):
        self.action("missing", "submitted")
        self.action("failed", "requested")
        self.action("failed", "submitted", False)
        self.action("mismatch", "requested")
        self.action("mismatch", "submitted", decision={**self.decision, "target_bssid": "other"})
        self.action("stale", "requested", world_epoch=2)
        self.action("stale", "submitted", world_epoch=2)
        self.assertEqual(self.store.mesh_layout()["steering_actions"], [])
        requested = self.action("good", "requested")
        self.assertEqual(self.store.mesh_layout()["steering_actions"], [])
        self.action("good", "submitted")
        action = self.store.mesh_layout()["steering_actions"][0]
        self.assertEqual(action["method"], "btm-request")
        self.assertEqual(action["evidence"], "native-acceptance")
        self.assertEqual(action["requested_at"], requested["recorded_at"])
        action["target_bssid"] = "modified"
        self.assertEqual(self.store.mesh_layout()["steering_actions"][0]["target_bssid"], self.decision["target_bssid"])

    def test_history_is_bounded_expires_and_cannot_cross_worlds(self):
        for index in range(105):
            self.action(str(index), "requested")
        self.assertEqual(len(self.store._steering_requests), 100)
        for index in range(110):
            self.action(str(index), "requested")
            self.action(str(index), "submitted")
        self.assertEqual(len(self.store.mesh_layout()["steering_actions"]), 100)
        stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=31)).isoformat()
        for action in self.store._steering_actions:
            action["requested_at"] = stale
        self.assertEqual(self.store.mesh_layout()["steering_actions"], [])
        self.action("new", "requested")
        self.action("new", "submitted")
        self.store.emit("room.world.committed", 0, {"world": self.world, "roles": {}})
        self.assertEqual(self.store.mesh_layout()["steering_actions"], [])
        self.assertEqual(self.store._steering_requests, {})


if __name__ == "__main__":
    unittest.main()
