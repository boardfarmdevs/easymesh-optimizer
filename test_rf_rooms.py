import json
from pathlib import Path

from wmdcfg.traffic_profile import validate_traffic
from wmdcfg.world import verify_world_plan
from room_demo.band_profiles import validate_profiles


ROOT = Path(__file__).resolve().parents[2] / "wmediumd" / "configurator" / "worlds"
ROOMS = ("traffic-low-high-off", "traffic-quieter-ap", "received-same-band-roam", "received-discovery-recovery")


def test_new_rf_rooms_are_short_signed_and_keep_the_existing_pool():
    for name in ROOMS:
        world = json.loads((ROOT / "golden" / f"{name}.world.json").read_text())
        verify_world_plan(world)
        assert world["duration_ms"] <= 36000
        assert world["counts"] == {"agents": 5, "stations": 10}
        assert world.get("backhaul_rf", "fixed") == "fixed"
        assert len(validate_profiles(world)) <= 2
        validate_traffic(world)


def test_received_visibility_room_disables_only_fronthaul_then_recovers():
    world = json.loads((ROOT / "golden" / "received-discovery-recovery.world.json").read_text())
    frames = {frame["time_ms"]: frame for frame in world["generations"]}
    assert not frames[12000]["present"]["extender_1"]
    assert frames[24000]["present"]["extender_1"]
    for frame in frames.values():
        assert frame["present"]["gateway"]
        assert all(frame["present"][role] for role, kind in world["roles"].items() if kind == "station")


def test_existing_traffic_rooms_offer_low_high_udp_with_off_intervals():
    for name in ROOMS[:2]:
        world = json.loads((ROOT / "golden" / f"{name}.world.json").read_text())
        mobility = json.loads((ROOT / "mobility" / f"{name}.json").read_text())
        profile = validate_traffic(world)
        assert profile == mobility["traffic_experiment"]
        assert [phase["mode"] for phase in profile["phases"]] == ["udp", "udp"]
        assert [phase["offered_mbps"] for phase in profile["phases"]] == [1, 8]
        assert [(phase["start_ms"], phase["end_ms"]) for phase in profile["phases"]] == [(5000, 10000), (10000, 23000)]
