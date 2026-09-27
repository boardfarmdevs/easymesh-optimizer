from concurrent.futures import ThreadPoolExecutor
import unittest

from room_demo.interactions import InteractionError
from room_demo.steering_safety import SteeringSafety


class SteeringSafetyTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.safety = SteeringSafety(clock=lambda: self.now)
        self.safety.sync(1, {"client": (1, 0), "other": (1, 0)})

    def issue(self, source="ap-a", target="ap-b", success=True, station="client"):
        ticket, reason = self.safety.reserve(station, source, target)
        self.assertIsNone(reason)
        self.assertIsNotNone(ticket)
        self.safety.complete(ticket, success, "verification_timeout" if not success else "verified")
        self.now += 5
        return ticket

    def test_continuous_requests_cross_the_old_lifetime_limit(self):
        for _index in range(350):
            self.issue()
        self.assertEqual(self.safety.snapshot()["total_requests"], 350)
        self.assertEqual(self.safety.snapshot()["state"], "ready")

    def test_rate_limit_is_rolling_and_does_not_pause_clients(self):
        self.safety = SteeringSafety(2, clock=lambda: self.now)
        self.issue(station="first")
        self.issue(station="second")
        self.assertEqual(self.safety.check("third", "ap-a", "ap-b"), "steering_rate_limited")
        status = self.safety.snapshot()
        self.assertEqual(status["paused_clients"], [])
        self.assertEqual(status["rate_retry_seconds"], 50)
        self.safety.resume(status["pause_revision"])
        self.assertEqual(self.safety.snapshot()["requests_in_window"], 2)
        self.now = 60
        self.assertIsNone(self.safety.check("third", "ap-a", "ap-b"))
        self.assertEqual(self.safety.snapshot()["requests_in_window"], 1)

    def test_cooldown_and_pending_request_do_not_block_other_clients(self):
        ticket, reason = self.safety.reserve("client", "ap-a", "ap-b")
        self.assertIsNone(reason)
        self.assertEqual(self.safety.check("client", "ap-a", "ap-b"), "steering_request_pending")
        self.assertIsNone(self.safety.check("other", "ap-a", "ap-b"))
        self.safety.complete(ticket, True)
        self.assertEqual(self.safety.check("client", "ap-b", "ap-a"), "steering_client_cooldown")
        self.safety.resume(self.safety.snapshot()["pause_revision"])
        self.assertEqual(self.safety.check("client", "ap-b", "ap-a"), "steering_client_cooldown")
        self.now = 5
        self.assertIsNone(self.safety.check("client", "ap-b", "ap-a"))

    def test_failures_pause_only_the_affected_client_and_resume_preserves_counters(self):
        for _index in range(3):
            self.issue(success=False)
        status = self.safety.snapshot()
        self.assertEqual(status["paused_clients"][0]["reason"], "steering_failures_paused")
        self.assertEqual(self.safety.check("client", "ap-a", "ap-b"), "steering_failures_paused")
        self.assertIsNone(self.safety.check("other", "ap-a", "ap-b"))
        resumed = self.safety.resume(status["pause_revision"])
        self.assertEqual(resumed["resumed_clients"], ["client"])
        self.assertEqual(resumed["total_requests"], 3)
        self.assertEqual(resumed["requests_in_window"], 3)
        self.assertIsNone(self.safety.check("client", "ap-a", "ap-b"))

    def test_success_breaks_consecutive_failures(self):
        self.issue(success=False)
        self.issue(success=False)
        self.issue()
        self.issue(success=False)
        self.assertEqual(self.safety.snapshot()["paused_clients"], [])

    def test_failures_are_windowed(self):
        self.issue(success=False)
        self.issue(success=False)
        self.now += 181
        self.issue(success=False)
        self.assertEqual(self.safety.snapshot()["paused_clients"], [])

    def test_oscillation_is_blocked_before_the_fourth_reversal_request(self):
        self.issue("ap-a", "ap-b")
        self.issue("ap-b", "ap-a")
        self.issue("ap-a", "ap-b")
        ticket, reason = self.safety.reserve("client", "ap-b", "ap-a")
        self.assertIsNone(ticket)
        self.assertEqual(reason, "steering_oscillation_paused")
        self.assertEqual(self.safety.snapshot()["total_requests"], 3)
        self.assertIsNone(self.safety.check("other", "ap-a", "ap-b"))

    def test_moving_room_and_old_reversals_are_not_oscillation(self):
        for index in range(8):
            self.safety.sync(1, {"client": (1, index)})
            self.issue("ap-a" if index % 2 else "ap-b", "ap-b" if index % 2 else "ap-a")
        self.now += 61
        self.issue("ap-a", "ap-b")
        self.assertEqual(self.safety.snapshot()["paused_clients"], [])

    def test_rf_change_does_not_clear_an_unrelated_pause(self):
        for _index in range(3):
            self.issue(success=False)
        self.safety.sync(1, {"client": (1, 0), "other": (1, 2)})
        self.assertEqual(len(self.safety.snapshot()["paused_clients"]), 1)
        self.safety.sync(1, {"client": (1, 1), "other": (1, 2)})
        self.assertEqual(self.safety.snapshot()["paused_clients"], [])
        self.assertEqual(self.safety.snapshot()["total_requests"], 3)

    def test_resume_rejects_unseen_protection_changes(self):
        revision = self.safety.snapshot()["pause_revision"]
        for _index in range(3):
            self.issue(success=False)
        with self.assertRaises(InteractionError) as caught:
            self.safety.resume(revision)
        self.assertEqual(caught.exception.code, "steering_safety_changed")
        self.assertEqual(len(self.safety.snapshot()["paused_clients"]), 1)

    def test_old_or_duplicate_results_cannot_repause_a_new_context(self):
        ticket, _reason = self.safety.reserve("client", "ap-a", "ap-b")
        self.safety.sync(1, {"client": (2, 0)})
        self.safety.complete(ticket, False)
        self.safety.complete(ticket, False)
        self.safety.complete(ticket, False)
        self.now += 5
        self.issue(success=False)
        self.assertEqual(self.safety.snapshot()["paused_clients"], [])

    def test_world_change_keeps_rate_and_client_spacing_but_discards_old_results(self):
        ticket, _reason = self.safety.reserve("client", "ap-a", "ap-b")
        self.safety.sync(2, {"client": (0, 0)})
        self.safety.complete(ticket, False)
        self.assertEqual(self.safety.check("client", "ap-a", "ap-b"), "steering_client_cooldown")
        self.assertEqual(self.safety.snapshot()["requests_in_window"], 1)

    def test_concurrent_reservations_cannot_exceed_the_global_limit(self):
        safety = SteeringSafety(5, clock=lambda: self.now)
        with ThreadPoolExecutor(max_workers=16) as executor:
            reservations = list(executor.map(lambda station: safety.reserve(str(station), "ap-a", "ap-b"), range(40)))
        self.assertEqual(sum(ticket is not None for ticket, _reason in reservations), 5)
        self.assertEqual(safety.snapshot()["requests_in_window"], 5)

    def test_invalid_limits_and_resume_revisions_are_rejected(self):
        for limit in [0, -1, True, 2.5]:
            with self.assertRaises(ValueError):
                SteeringSafety(limit)
        for revision in [True, None, "1", 1.5]:
            with self.assertRaises(InteractionError):
                self.safety.resume(revision)


if __name__ == "__main__":
    unittest.main()
