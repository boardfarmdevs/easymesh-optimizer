import unittest
from unittest import mock

from room_demo.cli import _interactive_preflight
from wmdcfg.actuator import ActuatorError


class RecoveryPreflightTests(unittest.TestCase):
    def check(self, errors, recovered=True, cancelled=False):
        conductor = mock.Mock()
        conductor.preflight.side_effect = errors
        stop = mock.Mock()
        stop.wait.return_value = cancelled
        with mock.patch("room_demo.cli.mesh_health", return_value={"healthy": True}), \
             mock.patch("room_demo.cli.Runner._require_healthy"):
            result = _interactive_preflight(conductor, 5, 20, recovered, stop)
        return result, conductor, stop

    def test_recovered_roster_waits_for_traffic_readiness(self):
        result, conductor, stop = self.check([
            RuntimeError("demo preflight failed: hero traffic probe failed"), None])
        self.assertTrue(result["healthy"])
        self.assertEqual(conductor.preflight.call_count, 2)
        stop.wait.assert_called_once_with(0.5)

    def test_normal_startup_does_not_add_a_retry_policy(self):
        with self.assertRaisesRegex(RuntimeError, "hero traffic"):
            self.check([RuntimeError("demo preflight failed: hero traffic")], recovered=False)

    def test_unexpected_faults_are_not_hidden(self):
        with self.assertRaisesRegex(RuntimeError, "unexpected"):
            self.check([RuntimeError("unexpected programming fault")])

    def test_recovery_wait_has_a_deadline_and_is_cancellable(self):
        with mock.patch("room_demo.cli.time.monotonic", side_effect=[0, 31]), \
             self.assertRaisesRegex(RuntimeError, "hero traffic"):
            self.check([RuntimeError("demo preflight failed: hero traffic")])
        with self.assertRaisesRegex(ActuatorError, "cancelled"):
            self.check([RuntimeError("demo preflight failed: hero traffic")], cancelled=True)
