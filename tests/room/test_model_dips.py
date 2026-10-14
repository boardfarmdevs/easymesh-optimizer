from __future__ import annotations

import unittest

from room_service.model_dips import BssDips, LinkSettle, health_verdict

ZERO = "00:00:00:00:00:00"
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

    def test_a_missing_row_keeps_its_kind_and_a_zero_bssid_is_shared_with_no_one(self):
        # two wired devices' link rows both read 00:00:00:00:00:00: no BSSID, not one they share
        rows = [{"device": "00:60:2f:da:68:e4", "band": "5", "bssid": ZERO, "ssid": "mesh_backhaul", "kind": "link"},
                {"device": "02:00:00:00:69:20", "band": "5", "bssid": ZERO, "ssid": "mesh_backhaul", "kind": "link"},
                {**ROWS[2], "kind": "bss"}]
        dips = BssDips()
        dips.observe(health(rows), T0)
        payload = health(rows[1:])
        dips.observe(payload, T0 + 1)
        self.assertEqual([(row["device"], row["kind"], row["shared_with"]) for row in payload["model_bss_missing"]],
                         [("00:60:2f:da:68:e4", "link", [])])

    def test_a_stack_without_rows_is_left_alone(self):
        dips = BssDips()
        payload = {"model_bsses": 1, "expected_model_bsses": 3}
        self.assertIsNone(dips.observe(payload, T0))
        self.assertNotIn("model_bss_missing", payload)
        self.assertFalse(dips.open)


# a settled lab of one agent (nine BSSes and its link row) and ten clients
SPLIT = {"api_active": 10, "topology_nodes": 3, "complete_nodes": 3,
         "model_devices": 1, "model_radios": 3, "model_bsses": 10, "model_ap_bsses": 9, "model_link_rows": 1,
         "model_associated": 10, "expected_model_devices": 1, "expected_model_radios": 3,
         "expected_model_bsses": 10, "expected_model_ap_bsses": 9, "expected_model_link_rows": 1,
         "expected_model_associated": 10}


class LinkSettleTests(unittest.TestCase):
    def test_the_link_rows_mismatch_is_timed_from_its_first_sample(self):
        settle = LinkSettle()
        self.assertEqual(settle.observe(SPLIT, 100.0), 0.0)
        short = {**SPLIT, "model_link_rows": 0}
        self.assertEqual(settle.observe(short, 101.0), 0.0)
        # a surplus is a mismatch too (a row added before the old one went)
        self.assertEqual(settle.observe({**SPLIT, "model_link_rows": 2}, 104.5), 3.5)
        self.assertEqual(settle.observe(SPLIT, 106.0), 0.0)
        self.assertEqual(settle.observe(short, 107.0), 0.0)

    def test_without_the_split_there_is_nothing_to_time(self):
        settle = LinkSettle()
        self.assertIsNone(settle.observe({"model_bsses": 10, "expected_model_bsses": 10}, 1.0))
        self.assertIsNone(settle.since)


class HealthVerdictTests(unittest.TestCase):
    def verdict(self, payload, link_short=0.0, settle=30.0):
        return health_verdict(payload, clients=10, nodes=3, link_short=link_short, settle=settle)

    def test_a_complete_model_is_healthy_both_ways(self):
        self.assertEqual(self.verdict(SPLIT), (True, True))

    def test_a_missing_access_point_bss_is_a_fault_at_once(self):
        self.assertEqual(self.verdict({**SPLIT, "model_bsses": 9, "model_ap_bsses": 8, "complete_nodes": 2}),
                         (False, False))

    def test_link_rows_settle_for_the_window_then_fail(self):
        short = {**SPLIT, "model_bsses": 9, "model_link_rows": 0, "complete_nodes": 2}
        self.assertEqual(self.verdict(short, link_short=0.0), (False, True))
        self.assertEqual(self.verdict(short, link_short=30.0), (False, True))
        self.assertEqual(self.verdict(short, link_short=30.5), (False, False))
        # no window: the raw verdict
        self.assertEqual(self.verdict(short, link_short=0.0, settle=0.0), (False, True))
        self.assertEqual(self.verdict(short, link_short=0.1, settle=0.0), (False, False))

    def test_settling_links_do_not_cover_anything_else(self):
        short = {**SPLIT, "model_bsses": 9, "model_link_rows": 0, "complete_nodes": 2}
        for other in ({"api_active": 9}, {"topology_nodes": 2}, {"model_ap_bsses": 8},
                      {"model_radios": 2}, {"model_devices": 0}, {"model_associated": 9}):
            with self.subTest(other=other):
                self.assertEqual(self.verdict({**short, **other}), (False, False))

    def test_without_the_split_the_verdict_is_the_one_count(self):
        # prplMesh, or a medium without link rows: model_bsses as before, nothing settles
        whole = {key: value for key, value in SPLIT.items() if "ap_bsses" not in key and "link_rows" not in key}
        self.assertEqual(self.verdict(whole, link_short=None), (True, True))
        self.assertEqual(self.verdict({**whole, "model_bsses": 9, "complete_nodes": 2}, link_short=None),
                         (False, False))
        prpl = {"api_active": 10, "topology_nodes": 3, "complete_nodes": 3}
        self.assertEqual(self.verdict(prpl, link_short=None), (True, True))


if __name__ == "__main__":
    unittest.main()
