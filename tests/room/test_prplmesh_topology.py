import unittest
from datetime import datetime, timezone

from room_service.topology import project_topology


class PrplMeshTopologyTests(unittest.TestCase):
    def test_native_backhaul_signal_requires_fresh_received_report_and_known_parent(self):
        now = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
        backhaul = {"parent_id": "AA:00", "type": "Wi-Fi", "mac": "BB:00",
                    "station_mac": "CC:01", "signal_dbm": -67}
        topology = {"devices": [
            {"id": "AA:00", "radios": [{"band": "5 GHz", "channel": 36,
                                          "bsses": [{"bssid": "BB:00"}]}]},
            {"id": "AA:01", "radios": [{"bsses": [{"bssid": "BB:01"}]}],
             "backhaul": backhaul}]}
        roles = {"bb:00": "gateway", "bb:01": "extender_1"}
        for timestamp, status in (("2026-09-22T11:59:59.123456789Z", "fresh"),
                                  ("2026-09-22T11:59:54Z", "stale"),
                                  ("2026-09-22T12:00:01Z", "invalid"), (None, "invalid")):
            with self.subTest(timestamp=timestamp):
                backhaul["signal_updated_at"] = timestamp
                edge = project_topology(topology, roles, now=now)["backhaul_edges"][0]
                self.assertEqual(edge["signal"]["status"], status)
                self.assertEqual(edge["signal"]["rssi_dbm"], -67 if status == "fresh" else None)
                self.assertEqual(edge["signal"]["observed_at"], timestamp)
                self.assertEqual(edge["backhaul_sta"], "cc:01")
        backhaul["mac"] = "unknown"
        self.assertEqual(project_topology(topology, roles, now=now)["backhaul_edges"][0]["signal"], {})

    def test_backhaul_uses_observed_parent_and_bssid_bound_roles(self):
        topology = {"devices": [
            {"id": "AA:00", "name": "controller", "radios": [
                {"bsses": [{"bssid": "BB:00"}]}]},
            {"id": "AA:01", "name": "discovery-order-not-a-role", "radios": [
                {"bsses": [{"bssid": "BB:01"}]}],
             "backhaul": {"parent_id": "AA:00", "type": "Wi-Fi", "mac": "CC:01"}},
        ]}
        projected = project_topology(topology, {"bb:00": "gateway", "bb:01": "extender_4"})
        self.assertEqual(projected["source"], "prplmesh_nbapi")
        self.assertEqual(projected["unresolved_edges"], 0)
        edge = projected["backhaul_edges"][0]
        self.assertEqual(edge["parent_role"], "gateway")
        self.assertEqual(edge["child_role"], "extender_4")
        self.assertEqual(edge["signal"], {})
        self.assertIsNone(edge["band"])

    def test_absent_or_unresolved_topology_is_not_invented(self):
        self.assertFalse(project_topology(None, {})["available"])
        projected = project_topology({"devices": [
            {"id": "child", "backhaul": {"parent_id": "unknown"}}
        ]}, {})
        self.assertEqual(projected["backhaul_edges"], [])
        self.assertEqual(projected["unresolved_edges"], 1)


class PrplMeshWiredBackhaulTests(unittest.TestCase):
    def test_an_agent_on_a_wired_backhaul_is_an_ethernet_child_not_a_wifi_edge(self):
        topology = {"devices": [
            {"id": "AA:00", "radios": [{"bsses": [{"bssid": "BB:00"}]}]},
            {"id": "AA:05", "radios": [{"bsses": [{"bssid": "BB:05"}]}],
             "backhaul": {"parent_id": "AA:00", "type": "Ethernet", "mac": ""}},
            {"id": "AA:02", "radios": [{"bsses": [{"bssid": "BB:02"}]}],
             "backhaul": {"parent_id": "AA:05", "type": "Wi-Fi", "mac": "BB:05"}},
        ]}
        projected = project_topology(topology, {"bb:00": "gateway", "bb:05": "extender_5", "bb:02": "extender_2"})
        self.assertEqual(projected["wired_edges"],
                         [{"parent_role": "gateway", "child_role": "extender_5", "media_type": "Ethernet"}])
        # a Wi-Fi Agent may hang off it
        self.assertEqual([(edge["parent_role"], edge["child_role"]) for edge in projected["backhaul_edges"]],
                         [("extender_5", "extender_2")])
        self.assertEqual(project_topology(None, {})["wired_edges"], [])
