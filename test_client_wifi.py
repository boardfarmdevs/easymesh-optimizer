from __future__ import annotations

import tempfile
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
import unittest
from unittest.mock import Mock, patch

from room_demo.client_wifi import disconnected_client, read_client_link, reconnect_client, resume_bound_client
from room_demo.recovery import RecoveryJournal, load_recovery, recover_medium
from wmdcfg.actuator import ActuatorError


class ClientWifiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "recovery.json"
        self.journal = RecoveryJournal(self.path, "run", "inventory")
        self.journal.prepare("medium", 3, {})
        self.plan = {"bindings": {"client": {"container": "wlan-client-012"}}}

    def test_disconnect_is_journaled_before_command_and_always_resumed(self):
        operations = []

        def execute(arguments, **_kwargs):
            self.assertEqual(load_recovery(self.path)["paused_clients"], ["wlan-client-012"])
            operations.append(arguments[-1])
            return CompletedProcess(arguments, 0, "OK\n", "")

        with patch("room_demo.client_wifi.subprocess.run", side_effect=execute):
            with self.assertRaises(RuntimeError):
                with disconnected_client(self.plan, "client", self.journal):
                    self.assertEqual(operations, ["disconnect"])
                    raise RuntimeError("RF apply rejected")
        self.assertEqual(operations, ["disconnect", "reconnect"])
        self.assertEqual(load_recovery(self.path)["paused_clients"], [])

    def test_failed_resume_remains_recoverable_after_rf_restoration(self):
        medium = Mock()
        medium.connect.return_value.instance_id = "medium"
        medium.connect.return_value.generation = 4
        self.journal.pause_client("wlan-client-012")
        self.journal.completed(4, True)
        with patch("room_demo.recovery.reconnect_client", side_effect=ActuatorError("unavailable")):
            with self.assertRaises(ActuatorError):
                recover_medium(self.path, "unused", client_factory=lambda _: medium)
        self.assertEqual(load_recovery(self.path)["paused_clients"], ["wlan-client-012"])
        with patch("room_demo.recovery.reconnect_client") as reconnect:
            self.assertEqual(recover_medium(self.path, "unused", client_factory=lambda _: medium)["status"], "already-restored")
        reconnect.assert_called_once_with("wlan-client-012")
        self.assertEqual(load_recovery(self.path)["paused_clients"], [])

    def test_offline_client_stays_paused_until_explicit_presence_restore(self):
        with patch("room_demo.client_wifi.subprocess.run", return_value=CompletedProcess([], 0, "OK\n", "")) as execute:
            with disconnected_client(self.plan, "client", self.journal):
                pass
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(self.journal.paused_clients(), ["wlan-client-012"])
            resume_bound_client(self.plan, "client", self.journal)
            self.assertEqual(execute.call_count, 2)
            self.assertEqual(self.journal.paused_clients(), [])

    def test_new_session_cannot_overwrite_pending_client_recovery(self):
        self.journal.pause_client("wlan-client-012")
        self.journal.completed(4, True)
        with self.assertRaises(ActuatorError):
            self.journal.prepare("medium", 4, {})

    def test_invalid_container_and_failed_control_reply(self):
        with patch("room_demo.client_wifi.subprocess.run") as execute:
            with self.assertRaises(ActuatorError):
                reconnect_client("bpibroadband")
            execute.assert_not_called()
            execute.return_value = CompletedProcess([], 0, "FAIL\n", "")
            with self.assertRaises(ActuatorError):
                reconnect_client("wlan-client-012")

    def test_kernel_sample_requires_successful_wlan_probe_and_exact_owner(self):
        link = "Connected to 02:00:00:00:01:01 (on wlan0)\n freq: 5180.0\n signal: -55 dBm\n"
        with patch("room_demo.client_wifi.subprocess.run", side_effect=[
            CompletedProcess([], 0, "reply", ""), CompletedProcess([], 0, link, ""),
        ]) as execute:
            sample = read_client_link("wlan-client-012", "02:00:00:00:01:01", "5", "10.0.0.1")
        self.assertEqual(sample["rcpi"], 110)
        self.assertEqual(sample["measurement_source"], "client_kernel_iw_link_after_wlan_traffic_probe")
        self.assertTrue(sample["metric_observed_at"])
        self.assertIn("wlan0", execute.call_args_list[0].args[0])
        self.assertEqual(execute.call_args_list[0].args[0][-1], "10.0.0.1")

    def test_kernel_sample_rejects_roam_band_mismatch_and_unavailable_signal(self):
        for link in (
            "Connected to 02:00:00:00:02:01 (on wlan0)\n freq: 5180\n signal: -55 dBm\n",
            "Connected to 02:00:00:00:01:01 (on wlan0)\n freq: 2437\n signal: -55 dBm\n",
            "Connected to 02:00:00:00:01:01 (on wlan0)\n freq: 5180\n signal: -999 dBm\n",
            "Connected to 02:00:00:00:01:01 (on wlan0)\n freq: 5180\n",
            "Not connected.\n",
        ):
            with self.subTest(link=link), patch("room_demo.client_wifi.subprocess.run", side_effect=[
                CompletedProcess([], 0, "reply", ""), CompletedProcess([], 0, link, ""),
            ]):
                self.assertIsNone(read_client_link("wlan-client-012", "02:00:00:00:01:01", "5", "10.0.0.1"))

    def test_kernel_probe_failure_timeout_and_nonclient_are_not_measurements(self):
        for failure in (CompletedProcess([], 1, "", ""), TimeoutExpired("ping", 3), OSError("unavailable")):
            with self.subTest(failure=failure), patch("room_demo.client_wifi.subprocess.run") as execute:
                if isinstance(failure, Exception):
                    execute.side_effect = failure
                else:
                    execute.return_value = failure
                self.assertIsNone(read_client_link("wlan-client-012", "02:00:00:00:01:01", "5", "10.0.0.1"))
                self.assertEqual(execute.call_count, 1)
        with patch("room_demo.client_wifi.subprocess.run") as execute:
            self.assertIsNone(read_client_link("bpibroadband", "02:00:00:00:01:01", "5", "10.0.0.1"))
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
