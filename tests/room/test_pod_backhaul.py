"""The OpenSync pods' backhaul in the geometry rooms: their station's RF keys and their moves."""
from __future__ import annotations

import re
import types
import unittest

from room_service.backhaul import PodBackhaul, channel_of
from room_service.interactions import InteractiveMediumSession, pod_station_keys

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


def pod(number, **extra):
    """An OpenSync pod's binding: its 5 GHz station on a radio of its own, its 2.4 GHz
    one on the fronthaul radio (easymesh-medium's inventory)."""
    stations = [
        {"interface": "bhaul-sta-50", "band": "5", "tx_mac": f"42:00:00:00:7{number}:00",
         "station_mac": f"02:00:00:15:00:0{number}"},
        {"interface": "bhaul-sta-24", "band": "2.4", "tx_mac": f"42:00:00:00:6{number}:00",
         "station_mac": f"02:00:00:14:00:0{number}"},
    ]
    return {**POD, "container": f"pod-{number}", "backhaul_station": stations[0],
            "backhaul_stations": stations, **extra}


def plan(*more):
    native = lambda container, **extra: {"role_type": "fronthaul_ap", "container": container, **extra}
    bindings = {
        "gateway": native("bpibroadband"), "extender_1": native("bpiap"),
        "extender_5": native("bpiap-004", backhaul="wired", wired_guard="hal"),
        "extender_6": native("bpiap-005", backhaul="wired"),  # unguarded: never a parent
        "pod_1": pod(1), "pod_2": pod(2),
        "sta_mobile_01": {"role_type": "station", "container": "wlan-client"},
    }
    for number in more:
        bindings[f"pod_{number}"] = pod(number)
    return {"bindings": bindings}


def link(source, destination, snr, reverse=None, band="5"):
    """A backhaul pair's links, both directions; ``band`` at ``snr``, the others far off."""
    def one(a, b, value):
        bands = {"2.4": -20, "5": -20, "6": -20}
        bands[band] = value
        return {"source_role": a, "destination_role": b, "link_class": "backhaul", "snr_db_by_band": bands}
    return [one(source, destination, snr), one(destination, source, snr if reverse is None else reverse)]


def world(*pairs):
    return {"generations": [{"links": [item for pair in pairs for item in pair]}]}


AGENTS = {number: f"02:72:00:00:00:0{number}" for number in range(1, 5)}
STATION_BAND = {"36": "bhaul-sta-50", "6": "bhaul-sta-24"}


class FakeLab:
    """The containers a PodBackhaul reads and the controller method it calls."""

    def __init__(self, pods=2):
        # each pod's station in use and its BSS: on the gateway at first
        self.links = {f"pod-{number}": ("bhaul-sta-50", "02:00:00:00:00:36") for number in range(1, pods + 1)}
        self.calls = []

    def __call__(self, arguments, timeout=10):
        self.calls.append(arguments)
        container, command = arguments[2], arguments[4:]
        if command[:2] == ["cat", "/sys/class/net/wifi1.1/address"]:
            return {"bpibroadband": "02:00:00:00:00:36\n", "bpiap": "02:00:00:00:01:36\n",
                    "bpiap-004": "02:00:00:00:05:36\n"}[container]
        if command[:2] == ["cat", "/sys/class/net/b-ap-24/address"]:
            return f"02:00:00:00:2{container[-1]}:24\n"
        if command[:2] == ["iw", "dev"]:
            interface, bssid = self.links[container]
            return f"Connected to {bssid} (on {interface})\n" if command[2] == interface else "Not connected.\n"
        if command[0] == "mysql":
            stations = re.findall(r"%([0-9a-f:]{17})%", command[-1])
            return "".join(f"OneWifiMesh@{AGENTS[int(station[-1])]}@{station}@02:00:00:00:00:36@1\n"
                           for station in stations)
        if command[:2] == ["rbuscli", "get"]:
            name = command[2]
            if name.endswith("DeviceNumberOfEntries"):
                return f"Value : {1 + len(self.links)}\r\n"
            index = int(name.split(".Device.")[1].split(".")[0])
            return f"Value : {(['02:00:00:00:00:10'] + list(AGENTS.values()))[index - 1]}\r\n"
        if command[:2] == ["rbuscli", "method_values"]:
            index = int(command[2].split(".Device.")[1].split(".")[0])
            # EMOSA moved the pod onto the target with its station on the target's band
            self.links[f"pod-{index - 1}"] = (STATION_BAND[command[8]], command[5])
            return "method succeeded\n"
        raise AssertionError(arguments)


def steers(lab):
    return [call for call in lab.calls if call[4:6] == ["rbuscli", "method_values"]]


class PodBackhaulTargetTests(unittest.TestCase):
    def test_each_pod_goes_to_the_native_ap_with_the_strongest_5_ghz_link(self):
        rooms = world(
            link("pod_1", "gateway", -1), link("pod_1", "extender_1", 15), link("pod_1", "extender_5", 9),
            link("pod_2", "gateway", -3), link("pod_2", "extender_5", 24), link("pod_2", "pod_1", 30, band="2.4"),
            link("pod_2", "extender_6", 40))
        self.assertEqual(PodBackhaul(plan()).targets(rooms), {"pod_1": "extender_1", "pod_2": "extender_5"})

    def test_a_links_weaker_direction_decides(self):
        rooms = world(link("pod_1", "gateway", 30, reverse=8), link("pod_1", "extender_1", 12),
                      link("pod_2", "gateway", 20))
        self.assertEqual(PodBackhaul(plan()).targets(rooms), {"pod_1": "extender_1", "pod_2": "gateway"})

    def test_on_a_tie_the_first_native_ap_by_name(self):
        rooms = world(link("pod_1", "gateway", 24), link("pod_1", "extender_1", 24), link("pod_2", "gateway", 24))
        self.assertEqual(PodBackhaul(plan()).targets(rooms), {"pod_1": "extender_1", "pod_2": "gateway"})

    def test_a_pod_out_of_every_native_aps_reach_goes_under_a_pod_on_2_4_ghz(self):
        # pod_2 hears the gateway at 4 dB, under the usable 5: pod_1 is its parent
        rooms = world(link("pod_1", "gateway", 25), link("pod_2", "gateway", 4),
                      link("pod_2", "pod_1", 20, band="2.4"))
        pods = PodBackhaul(plan())
        self.assertEqual(pods.targets(rooms), {"pod_1": "gateway", "pod_2": "pod_1"})
        self.assertEqual((pods.band_of("pod_1"), pods.channel("pod_1")), ("2.4", 6))
        self.assertEqual((pods.band_of("gateway"), pods.channel("gateway")), ("5", 36))

    def test_a_pod_out_of_reach_of_every_ap_stays_where_it_is(self):
        rooms = world(link("pod_1", "gateway", 25), link("pod_2", "gateway", 2),
                      link("pod_2", "pod_1", 4, band="2.4"))
        self.assertEqual(PodBackhaul(plan()).targets(rooms), {"pod_1": "gateway"})

    def test_the_pod_parent_with_the_best_path_less_3_db_a_hop(self):
        # pod_3: under pod_1 (bottleneck min(18, 30) = 18, one hop more: 15) or pod_2
        # (min(28, 22) = 22, under pod_1 itself: bottleneck min(22, 30), two hops more: 16)
        rooms = world(link("pod_1", "gateway", 30), link("pod_2", "gateway", 3), link("pod_3", "gateway", 1),
                      link("pod_2", "pod_1", 22, band="2.4"), link("pod_3", "pod_1", 18, band="2.4"),
                      link("pod_3", "pod_2", 28, band="2.4"))
        self.assertEqual(PodBackhaul(plan(3)).targets(rooms), {"pod_1": "gateway", "pod_2": "pod_1", "pod_3": "pod_2"})
        # pod_1's link to pod_3 better than pod_2's path: pod_3 goes there
        rooms = world(link("pod_1", "gateway", 30), link("pod_2", "gateway", 3), link("pod_3", "gateway", 1),
                      link("pod_2", "pod_1", 22, band="2.4"), link("pod_3", "pod_1", 21, band="2.4"),
                      link("pod_3", "pod_2", 28, band="2.4"))
        self.assertEqual(PodBackhaul(plan(3)).targets(rooms)["pod_3"], "pod_1")

    def test_pods_that_hear_only_each_other_have_no_parent(self):
        rooms = world(link("pod_1", "gateway", 2), link("pod_2", "gateway", 1), link("pod_2", "pod_1", 30, band="2.4"))
        self.assertEqual(PodBackhaul(plan()).targets(rooms), {})

    def test_a_pod_parent_on_another_2_4_ghz_channel_is_out(self):
        # the child's 2.4 GHz station shares its fronthaul radio and channel
        bindings = plan()
        bindings["bindings"]["pod_1"] = pod(1, fronthaul_frequencies_mhz={"2.4": 2412})
        rooms = world(link("pod_1", "gateway", 25), link("pod_2", "gateway", 3),
                      link("pod_2", "pod_1", 20, band="2.4"))
        self.assertEqual(PodBackhaul(bindings).targets(rooms), {"pod_1": "gateway"})

    def test_a_pod_without_a_2_4_ghz_station_has_no_pod_parent(self):
        bindings = plan()
        bindings["bindings"]["pod_2"] = {**POD, "container": "pod-2", "backhaul_station": pod(2)["backhaul_station"]}
        rooms = world(link("pod_1", "gateway", 25), link("pod_2", "gateway", 3),
                      link("pod_2", "pod_1", 20, band="2.4"))
        self.assertEqual(PodBackhaul(bindings).targets(rooms), {"pod_1": "gateway"})


class PodBackhaulMoveTests(unittest.TestCase):
    def test_a_move_goes_through_the_controller_and_waits_for_the_station(self):
        lab = FakeLab()
        pods = PodBackhaul(plan(), run=lab, sleep=lambda _: None)
        result = pods.arrange(world(link("pod_1", "extender_1", 15), link("pod_2", "gateway", 30)))
        by_pod = {item["pod"]: item for item in result["pods"]}
        self.assertEqual((by_pod["pod_1"]["bssid"], by_pod["pod_1"]["moved"]), ("02:00:00:00:01:36", True))
        self.assertEqual(by_pod["pod_2"]["moved"], False)  # already on the gateway
        steer = steers(lab)
        self.assertEqual(len(steer), 1)
        self.assertEqual(steer[0][6], "Device.WiFi.DataElements.Network.Device.2."
                         "MultiAPDevice.Backhaul.SteerWiFiBackhaul()")
        self.assertEqual(steer[0][7:], ["TargetBSS", "string", "02:00:00:00:01:36", "Channel", "int32", "36",
                                        "TimeOut", "int32", "30"])

    def test_a_pod_goes_under_a_pod_after_its_parent_on_the_parents_2_4_ghz_bss(self):
        lab = FakeLab(pods=3)
        pods = PodBackhaul(plan(3), run=lab, sleep=lambda _: None)
        rooms = world(link("pod_1", "extender_1", 25), link("pod_2", "gateway", 2), link("pod_3", "gateway", 1),
                      link("pod_2", "pod_1", 20, band="2.4"), link("pod_3", "pod_2", 28, band="2.4"))
        result = pods.arrange(rooms)
        self.assertEqual([(item["pod"], item["parent"], item["bssid"]) for item in result["pods"]], [
            ("pod_1", "extender_1", "02:00:00:00:01:36"),
            ("pod_2", "pod_1", "02:00:00:00:21:24"),
            ("pod_3", "pod_2", "02:00:00:00:22:24"),
        ])
        self.assertEqual([(call[9], call[12]) for call in steers(lab)],
                         [("02:00:00:00:01:36", "36"), ("02:00:00:00:21:24", "6"), ("02:00:00:00:22:24", "6")])
        # the pods' stations: pod_1 on 5 GHz, the others on their 2.4 GHz ones
        self.assertEqual({container: interface for container, (interface, _) in lab.links.items()},
                         {"pod-1": "bhaul-sta-50", "pod-2": "bhaul-sta-24", "pod-3": "bhaul-sta-24"})

    def test_a_pod_on_its_2_4_ghz_station_is_found_and_moved_back_to_a_native_ap(self):
        lab = FakeLab()
        lab.links["pod-2"] = ("bhaul-sta-24", "02:00:00:00:21:24")
        pods = PodBackhaul(plan(), run=lab, sleep=lambda _: None)
        self.assertEqual(pods.parent("pod_2"), "02:00:00:00:21:24")
        self.assertEqual(pods.device("pod_2"), 3)
        result = pods.move("pod_2", "gateway")
        self.assertEqual((result["moved"], lab.links["pod-2"]), (True, ("bhaul-sta-50", "02:00:00:00:00:36")))
        # the controller's rows of either station name the pod's agent
        query = [call for call in lab.calls if call[4] == "mysql"][0][-1]
        self.assertIn("02:00:00:15:00:02", query)
        self.assertIn("02:00:00:14:00:02", query)

    def test_outside_the_rooms_that_model_the_backhaul_a_chained_pod_goes_back_to_the_gateway(self):
        lab = FakeLab(pods=3)
        lab.links["pod-1"] = ("bhaul-sta-50", "02:00:00:00:01:36")  # under extender_1: stays
        lab.links["pod-2"] = ("bhaul-sta-24", "02:00:00:00:21:24")  # under pod_1
        lab.links["pod-3"] = ("bhaul-sta-24", "02:00:00:00:22:24")  # under pod_2
        pods = PodBackhaul(plan(3), run=lab, sleep=lambda _: None)
        self.assertEqual(pods.chained(), {"pod_2": "pod_1", "pod_3": "pod_2"})
        # the room's links are not read: it does not model the backhaul
        result = pods.arrange({"generations": [{"links": []}]}, models_backhaul=False)
        self.assertEqual([(item["pod"], item["parent"], item["moved"]) for item in result["pods"]],
                         [("pod_2", "gateway", True), ("pod_3", "gateway", True)])
        self.assertEqual([(call[9], call[12]) for call in steers(lab)],
                         [("02:00:00:00:00:36", "36"), ("02:00:00:00:00:36", "36")])
        self.assertEqual(lab.links["pod-1"], ("bhaul-sta-50", "02:00:00:00:01:36"))
        self.assertEqual(pods.arrange({"generations": [{"links": []}]}, models_backhaul=False), {"pods": []})

    def test_a_pod_image_without_a_2_4_ghz_backhaul_is_never_chained(self):
        lab = FakeLab()

        def run(arguments, timeout=10):
            if "b-ap-24" in " ".join(arguments) or "bhaul-sta-24" in arguments:
                raise RuntimeError("No such device")
            return lab(arguments, timeout)

        pods = PodBackhaul(plan(), run=run, sleep=lambda _: None)
        self.assertEqual(pods.chained(), {})
        self.assertEqual(pods.parent("pod_1"), "02:00:00:00:00:36")

    def test_a_pod_that_does_not_arrive_fails_the_move(self):
        lab = FakeLab()
        now = [0.0]
        pods = PodBackhaul(plan(), run=lambda arguments, timeout=10: (
            "method succeeded\n" if arguments[4:6] == ["rbuscli", "method_values"] else lab(arguments, timeout)),
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds), clock=lambda: now[0], timeout=10)
        with self.assertRaisesRegex(RuntimeError, "did not move to extender_1"):
            pods.move("pod_1", "extender_1")


class CatalogTests(unittest.TestCase):
    def test_the_catalog_names_the_adapters_aps_and_their_containers(self):
        session = types.SimpleNamespace(worlds=types.SimpleNamespace(catalog=lambda: {"worlds": []}),
                                        plan=plan(), world={"wired_backhaul": ["extender_5"]})
        catalog = InteractiveMediumSession.world_catalog(session)
        self.assertEqual(catalog["adapter_bindings"], {"pod_1": "pod-1", "pod_2": "pod-2"})
        self.assertEqual(catalog["wired_bindings"], {"extender_5": "bpiap-004"})


class ChannelTests(unittest.TestCase):
    def test_the_channel_of_each_bands_frequency(self):
        self.assertEqual([channel_of(f) for f in (2412, 2437, 2484, 5180, 5220, 5745, 5955, 5975)],
                         [1, 6, 14, 36, 44, 149, 1, 5])


if __name__ == "__main__":
    unittest.main()
