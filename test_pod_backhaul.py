"""The OpenSync pods' backhaul in the geometry rooms: their station's RF keys and their moves."""
from __future__ import annotations

import unittest

from room_demo.backhaul import PodBackhaul
from room_demo.interactions import pod_station_keys

POD = {
    "role_type": "fronthaul_ap", "container": "pod-1", "adapter": "emosa",
    "radio_tx_mac": "42:00:00:00:71:00",
    "band_radios": {"2.4": {"tx_mac": "42:00:00:00:71:00"}},
    "fronthaul_frequencies_mhz": {"2.4": 2437},
    "backhaul_station": {"interface": "bhaul-sta-50", "tx_mac": "42:00:00:00:72:00"},
}
NATIVE = {
    "role_type": "fronthaul_ap", "container": "bpiap",
    "radio_tx_mac": "42:00:00:00:01:00",
    "band_radios": {band: {"tx_mac": "42:00:00:00:01:00"} for band in ("2.4", "5", "6")},
    "fronthaul_frequencies_mhz": {"2.4": 2437, "5": 5180, "6": 5975},
}
OUT = {"snr_db_by_band": {"2.4": 30, "5": 21, "6": 18}}
IN = {"snr_db_by_band": {"2.4": 29, "5": 20, "6": 17}}


class PodStationKeyTests(unittest.TestCase):
    def test_a_pod_and_a_native_ap_have_station_keys_on_the_native_5_ghz_channel(self):
        keys = pod_station_keys(POD, NATIVE, OUT, IN)
        self.assertEqual(keys, [
            {"source": "42:00:00:00:72:00", "destination": "42:00:00:00:01:00",
             "frequency_mhz": 5180, "value": 21, "override": True},
            {"source": "42:00:00:00:01:00", "destination": "42:00:00:00:72:00",
             "frequency_mhz": 5180, "value": 20, "override": True},
        ])

    def test_seen_from_the_native_ap_the_directions_follow_the_pair(self):
        keys = pod_station_keys(NATIVE, POD, OUT, IN)
        # outgoing is native -> pod here: the pod's station hears the native AP at 21 dB
        self.assertEqual([(k["source"], k["value"]) for k in keys],
                         [("42:00:00:00:72:00", 20), ("42:00:00:00:01:00", 21)])

    def test_no_station_keys_between_two_pods_or_two_native_aps(self):
        other = {**POD, "container": "pod-2",
                 "backhaul_station": {"interface": "bhaul-sta-50", "tx_mac": "42:00:00:00:74:00"}}
        self.assertEqual(pod_station_keys(POD, other, OUT, IN), [])
        self.assertEqual(pod_station_keys(NATIVE, dict(NATIVE), OUT, IN), [])

    def test_a_pod_without_a_recorded_station_has_none(self):
        bare = {key: value for key, value in POD.items() if key != "backhaul_station"}
        self.assertEqual(pod_station_keys(bare, NATIVE, OUT, IN), [])


def plan():
    native = lambda container, **extra: {"role_type": "fronthaul_ap", "container": container, **extra}
    return {"bindings": {
        "gateway": native("bpibroadband"), "extender_1": native("bpiap"),
        "extender_5": native("bpiap-004", backhaul="wired", wired_guard="hal"),
        "extender_6": native("bpiap-005", backhaul="wired"),  # unguarded: never a parent
        "pod_1": {**POD, "backhaul_station": {**POD["backhaul_station"], "station_mac": "02:00:00:15:00:01"}},
        "pod_2": {**POD, "container": "pod-2", "backhaul_station": {
            "interface": "bhaul-sta-50", "tx_mac": "42:00:00:00:74:00", "station_mac": "02:00:00:15:00:02"}},
        "sta_mobile_01": {"role_type": "station", "container": "wlan-client"},
    }}


def link(source, destination, snr):
    return {"source_role": source, "destination_role": destination, "link_class": "backhaul",
            "snr_db_by_band": {"2.4": snr + 4, "5": snr, "6": snr - 3}}


class FakeLab:
    """The containers a PodBackhaul reads and the controller method it calls."""

    def __init__(self):
        self.parents = {"pod-1": "02:00:00:00:00:36", "pod-2": "02:00:00:00:00:36"}
        self.calls = []

    def __call__(self, arguments, timeout=10):
        self.calls.append(arguments)
        container, command = arguments[2], arguments[4:]
        if command[:2] == ["cat", "/sys/class/net/wifi1.1/address"]:
            return {"bpibroadband": "02:00:00:00:00:36\n", "bpiap": "02:00:00:00:01:36\n",
                    "bpiap-004": "02:00:00:00:05:36\n"}[container]
        if command[:2] == ["iw", "dev"]:
            return f"Connected to {self.parents[container]} (on bhaul-sta-50)\n"
        if command[0] == "mysql":
            station = command[-1].split("%")[1]
            agent = {"02:00:00:15:00:01": "02:72:00:00:00:01", "02:00:00:15:00:02": "02:72:00:00:00:02"}[station]
            return f"OneWifiMesh@{agent}@{station}@02:00:00:00:00:36@1\n"
        if command[:2] == ["rbuscli", "get"]:
            name = command[2]
            if name.endswith("DeviceNumberOfEntries"):
                return "Value : 3\r\n"
            index = int(name.split(".Device.")[1].split(".")[0])
            return f"Value : {['02:00:00:00:00:10', '02:72:00:00:00:01', '02:72:00:00:00:02'][index - 1]}\r\n"
        if command[:2] == ["rbuscli", "method_values"]:
            index = int(command[2].split(".Device.")[1].split(".")[0])
            self.parents[f"pod-{index - 1}"] = command[5]  # EMOSA moved the station
            return "method succeeded\n"
        raise AssertionError(arguments)


class PodBackhaulTests(unittest.TestCase):
    def test_each_pod_goes_to_the_native_ap_with_the_strongest_5_ghz_link(self):
        world = {"generations": [{"links": [
            link("pod_1", "gateway", -1), link("pod_1", "extender_1", 15), link("pod_1", "extender_5", 9),
            link("pod_2", "gateway", -3), link("pod_2", "extender_5", 24), link("pod_2", "pod_1", 30),
            link("pod_2", "extender_6", 40), link("extender_1", "pod_1", 99),
        ]}]}
        self.assertEqual(PodBackhaul(plan()).targets(world), {"pod_1": "extender_1", "pod_2": "extender_5"})

    def test_a_move_goes_through_the_controller_and_waits_for_the_station(self):
        lab = FakeLab()
        pods = PodBackhaul(plan(), run=lab, sleep=lambda _: None)
        world = {"generations": [{"links": [link("pod_1", "extender_1", 15), link("pod_2", "gateway", 30)]}]}
        result = pods.arrange(world)
        by_pod = {item["pod"]: item for item in result["pods"]}
        self.assertEqual((by_pod["pod_1"]["bssid"], by_pod["pod_1"]["moved"]), ("02:00:00:00:01:36", True))
        self.assertEqual(by_pod["pod_2"]["moved"], False)  # already on the gateway
        steer = [call for call in lab.calls if call[4:6] == ["rbuscli", "method_values"]]
        self.assertEqual(len(steer), 1)
        self.assertEqual(steer[0][6], "Device.WiFi.DataElements.Network.Device.2."
                         "MultiAPDevice.Backhaul.SteerWiFiBackhaul()")
        self.assertEqual(steer[0][7:], ["TargetBSS", "string", "02:00:00:00:01:36", "Channel", "int32", "36",
                                        "TimeOut", "int32", "30"])

    def test_a_pod_that_does_not_arrive_fails_the_move(self):
        lab = FakeLab()
        now = [0.0]
        pods = PodBackhaul(plan(), run=lambda arguments, timeout=10: (
            "method succeeded\n" if arguments[4:6] == ["rbuscli", "method_values"] else lab(arguments, timeout)),
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds), clock=lambda: now[0], timeout=10)
        with self.assertRaisesRegex(RuntimeError, "did not move to extender_1"):
            pods.move("pod_1", "extender_1")


if __name__ == "__main__":
    unittest.main()
