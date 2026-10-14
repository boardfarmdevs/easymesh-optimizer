from __future__ import annotations

import unittest

from room_service.model_dips import BssDips

ROWS = [
    {"device": "00:60:2f:da:68:e4", "band": "5", "bssid": "02:00:00:61:4f:f3", "ssid": "mesh_backhaul"},
    {"device": "02:00:00:00:01:20", "band": "5", "bssid": "02:00:00:61:4f:f3", "ssid": "mesh_backhaul"},
    {"device": "02:00:00:00:01:20", "band": "2.4", "bssid": "02:00:00:21:76:0e", "ssid": "iot_ssid"},
]
# 06:00:00 UTC on 14 October 2026
T0 = 1791957600.0


def health(rows, expected=3):
    return {"model_bsses": len(rows), "expected_model_bsses": expected, "model_bss_rows": list(rows)}


class BssDipTests(unittest.TestCase):
    def test_a_complete_sample_names_nothing_and_closes_nothing(self):
        dips = BssDips()
        payload = health(ROWS)
        self.assertIsNone(dips.observe(payload, T0))
        self.assertEqual(payload["model_bss_missing"], [])
        self.assertNotIn("model_bss_rows", payload)
        self.assertFalse(dips.open)

    def test_a_short_sample_names_the_bss_the_last_complete_one_had(self):
        dips = BssDips()
        dips.observe(health(ROWS), T0)
        payload = health(ROWS[:2])
        self.assertIsNone(dips.observe(payload, T0 + 10))
        self.assertTrue(dips.open)
        self.assertEqual(payload["model_bss_missing"], [
            {"device": "02:00:00:00:01:20", "band": "2.4", "bssid": "02:00:00:21:76:0e", "ssid": "iot_ssid",
             "shared_with": [], "since": "2026-10-14T06:00:10Z"}])

    def test_a_backhaul_links_row_names_the_other_device_with_its_bssid(self):
        # a child lists its parent's BSSID (its backhaul link): the row is shared with the parent's own
        dips = BssDips()
        dips.observe(health(ROWS), T0)
        payload = health([ROWS[0], ROWS[2]])
        dips.observe(payload, T0 + 10)
        self.assertEqual([(row["device"], row["shared_with"]) for row in payload["model_bss_missing"]],
                         [("02:00:00:00:01:20", ["00:60:2f:da:68:e4"])])

    def test_the_dip_closes_on_the_next_complete_sample_with_its_bounds(self):
        dips = BssDips()
        dips.observe(health(ROWS), T0)
        dips.observe(health(ROWS[:2]), T0 + 10)
        dips.observe(health(ROWS[:1]), T0 + 11)
        dips.observe(health(ROWS[:2]), T0 + 12)
        closed = dips.observe(health(ROWS), T0 + 13)
        self.assertFalse(dips.open)
        self.assertEqual({key: closed[key] for key in (
            "last_complete", "first_short", "last_short", "first_complete", "expected", "lowest",
            "min_seconds", "max_seconds")}, {
            "last_complete": "2026-10-14T06:00:00Z", "first_short": "2026-10-14T06:00:10Z",
            "last_short": "2026-10-14T06:00:12Z", "first_complete": "2026-10-14T06:00:13Z",
            "expected": 3, "lowest": 1, "min_seconds": 2.0, "max_seconds": 13.0})
        # every BSS missing at some point in the dip, with the first and last sample it was missing in
        self.assertEqual([(row["bssid"], row["device"], row["first"], row["last"]) for row in closed["missing"]], [
            ("02:00:00:21:76:0e", "02:00:00:00:01:20", "2026-10-14T06:00:10Z", "2026-10-14T06:00:12Z"),
            ("02:00:00:61:4f:f3", "02:00:00:00:01:20", "2026-10-14T06:00:11Z", "2026-10-14T06:00:11Z")])

    def test_a_dip_before_any_complete_sample_has_counts_but_no_names(self):
        dips = BssDips()
        payload = health(ROWS[:2])
        dips.observe(payload, T0)
        self.assertEqual(payload["model_bss_missing"], [])
        closed = dips.observe(health(ROWS), T0 + 1)
        self.assertEqual((closed["last_complete"], closed["max_seconds"], closed["missing"]), (None, None, []))

    def test_more_bsses_than_expected_close_a_dip_but_are_no_reference(self):
        dips = BssDips()
        dips.observe(health(ROWS), T0)
        dips.observe(health(ROWS[:2]), T0 + 1)
        extra = {"device": "02:00:00:00:02:20", "band": "6", "bssid": "02:00:00:00:00:01", "ssid": "x"}
        self.assertIsNotNone(dips.observe(health([*ROWS, extra]), T0 + 2))
        payload = health(ROWS[:2])
        dips.observe(payload, T0 + 3)
        self.assertEqual([row["bssid"] for row in payload["model_bss_missing"]], ["02:00:00:21:76:0e"])

    def test_a_stack_without_rows_is_left_alone(self):
        dips = BssDips()
        payload = {"model_bsses": 1, "expected_model_bsses": 3}
        self.assertIsNone(dips.observe(payload, T0))
        self.assertNotIn("model_bss_missing", payload)
        self.assertFalse(dips.open)


if __name__ == "__main__":
    unittest.main()
