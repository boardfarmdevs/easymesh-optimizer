import json
from pathlib import Path

from optimizer.rf_coverage import DEMONSTRATIONS
from optimizer.rf_observations import DEFINITIONS, property_catalog
from optimizer.config import load_policy
from room_demo.conductor import load_manifest
from room_demo.cli import DEFAULT_MANIFEST
from room_demo.worlds import BoundWorlds
from wmdcfg.world import compile_world, verify_world_plan


ROOT = Path(__file__).resolve().parents[3]
WORLDS = ROOT / "gen/wmediumd/configurator/worlds"
NEW_ROOMS = ("rf-packet-size-counters", "rf-asymmetric-ack")


def test_named_manifest_operates_counter_guard_without_changing_default_policy():
    manifest = load_manifest(ROOT / "gen/demo/manifests/native-counter-guard-room-profile.json", ROOT)
    config = load_policy(ROOT / manifest["policy"])
    world = json.loads((ROOT / manifest["world"]).read_text())
    assert config.load_counter_guard_enabled and config.load_aware_enabled
    assert world["mobility"] == "rf-asymmetric-ack"
    assert manifest["hero"]["role"] == world["traffic_experiment"]["phases"][0]["role"]
    assert manifest["health"]["expected_clients"] == world["counts"]["stations"]
    assert manifest["optimizer"]["request_only"]
    default = load_manifest(DEFAULT_MANIFEST, ROOT)
    assert not load_policy(ROOT / default["policy"]).load_counter_guard_enabled


def read(relative):
    return json.loads((WORLDS / relative).read_text())


def test_every_property_has_named_rooms_and_an_explicit_check_in_maintained_docs():
    assert set(DEMONSTRATIONS) == set(DEFINITIONS)
    document = (ROOT / "doc/easymesh/reference/radio/rf-property-coverage.md").read_text()
    for row in property_catalog()["properties"]:
        demo = row["demonstration"]
        assert demo["check"] and demo["expectation"] and demo["rooms"]
        assert demo["qualification"] == "contract_defined_live_evidence_required"
        assert f'`{row["id"]}`' in document
        for name in demo["rooms"]:
            assert name in document
            verify_world_plan(read(f"golden/{name}.world.json"))
        if row["usage"] == "unsupported":
            assert demo["check"] == "unsupported"
        if demo["check"] == "counter_guard":
            assert row["usage"] == "load_guard_opt_in"


def test_new_rooms_compile_reproducibly_and_select_from_existing_bound_pool():
    baseline = read("golden/home-a-private-client-room-walk.world.json")
    selector = BoundWorlds(baseline, read("layouts/home-five-agent.json"), WORLDS)
    for name in NEW_ROOMS:
        world, layout = selector.select(name)
        compiled = compile_world(layout, read(f"mobility/{name}.json"))
        installed = read(f"golden/{name}.world.json")
        assert compiled == installed
        assert world["counts"] == {"agents": 5, "stations": 10}
        assert world["duration_ms"] == 30000
        assert world.get("backhaul_rf", "fixed") == "fixed"
        assert all(all(frame["present"].values()) for frame in world["generations"])
        assert all(frame["positions"] == world["generations"][0]["positions"] for frame in world["generations"])


def test_packet_size_changes_without_geometry_or_offered_bitrate_changes():
    world = read("golden/rf-packet-size-counters.world.json")
    phases = world["traffic_experiment"]["phases"]
    assert [phase["payload_bytes"] for phase in phases] == [256, 1200]
    assert {phase["offered_mbps"] for phase in phases} == {1}
    assert phases[0]["end_ms"] < phases[1]["start_ms"]
    assert phases[-1]["end_ms"] < world["duration_ms"]


def test_asymmetric_room_changes_only_station_to_ap_direction():
    plain = read("golden/rf-packet-size-counters.world.json")["generations"][0]
    impaired = read("golden/rf-asymmetric-ack.world.json")["generations"][0]
    reference = {(row["source_role"], row["destination_role"]): row for row in plain["links"]}
    changed = 0
    for link in impaired["links"]:
        pair = (link["source_role"], link["destination_role"])
        for band, value in link["snr_db_by_band"].items():
            original = reference[pair]["snr_db_by_band"][band]
            if pair[0] == "sta_static_03":
                assert value == max(-20, original - 45)
                changed += 1
            else:
                assert value == original
    assert changed == 15
