from __future__ import annotations

import copy
import unittest

from room_demo.recovery import inventory_identity
from wmdcfg.actuator import ActuatorError


INVENTORY = {
    "schema": "wmdcfg.inventory.v1", "captured_at": "first",
    "radios": [
        {"container": "client", "kind": "station", "permanent_mac": "02:00:00:00:01:01",
         "tx_mac": "02:00:00:00:01:02", "station_mac": "02:00:00:00:01:03",
         "associated_bssid": "02:00:00:00:02:01", "frequency_mhz": 5180, "ssid": "private",
         "interfaces": [{"name": "wlan0", "mac": "02:00:00:00:01:03", "phy": "phy1",
                         "type": "managed", "frequency_mhz": 5180, "ssid": "private"}]},
        {"container": "ap", "kind": "mesh", "tx_mac": "02:00:00:00:02:02",
         "band_radios": {"5": {"phy": "phy2", "tx_mac": "02:00:00:00:02:02",
                               "frequency_mhz": 5180}}},
    ],
}


class RecoveryIdentityTests(unittest.TestCase):
    def test_discovery_time_and_client_roaming_do_not_change_identity(self):
        changed = copy.deepcopy(INVENTORY)
        changed["captured_at"] = "later"
        station = changed["radios"][0]
        station.update(associated_bssid=None, frequency_mhz=None, ssid=None)
        station["interfaces"][0].pop("frequency_mhz")
        station["interfaces"][0].pop("ssid")
        changed["radios"].reverse()
        self.assertEqual(inventory_identity(INVENTORY), inventory_identity(changed))

    def test_radio_replacement_and_ap_channel_changes_are_rejected(self):
        for field in ("container", "tx_mac", "station_mac"):
            with self.subTest(field=field):
                changed = copy.deepcopy(INVENTORY)
                changed["radios"][0][field] = "replacement"
                self.assertNotEqual(inventory_identity(INVENTORY), inventory_identity(changed))
        changed = copy.deepcopy(INVENTORY)
        changed["radios"][1]["band_radios"]["5"]["frequency_mhz"] = 5200
        self.assertNotEqual(inventory_identity(INVENTORY), inventory_identity(changed))

    def test_missing_and_duplicate_container_identity_fail_closed(self):
        for radios in ([], [INVENTORY["radios"][0]] * 2, [{"kind": "station"}]):
            with self.subTest(radios=radios), self.assertRaises(ActuatorError):
                inventory_identity({"radios": radios})


if __name__ == "__main__":
    unittest.main()
