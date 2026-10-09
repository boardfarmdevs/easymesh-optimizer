from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location("rf_property_rooms_smoke", Path(__file__).with_name("rf-property-rooms-smoke.py"))
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def observation():
    client = {"sta_mac": "02:00:00:00:03:00", "connected_bssid": "02:00:00:00:01:01"}
    identity = {"sta_mac": client["sta_mac"], "bssid": client["connected_bssid"], "epoch": "live"}
    record = {"property": "retries_per_second", "state": "valid", "value": 0,
              "identity": identity, "window_seconds": 1, "source": "native_sta_traffic",
              "transport": "ieee1905-ethernet", "observed_at": "2026-09-21T12:00:00Z"}
    inspection = {"enabled": True, "observations": {"decision_inputs": False, "records": [record]},
                  "bss_loads": [{"bssid": identity["bssid"], "epoch": "live", "context_state": "verified",
                                 "source": "native_ap_metrics", "transport": "ieee1905-ethernet"}]}
    return inspection, client, datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc).timestamp()


def test_live_check_preserves_zero_and_rejects_stale_foreign_owner_and_context():
    assert len(MODULE.native_records(*observation())) == 1
    for change in ("stale", "owner", "epoch", "context", "transport", "missing", "window", "fixture"):
        inspection, client, now = observation()
        record = inspection["observations"]["records"][0]
        if change == "stale":
            now += 6
        elif change == "owner":
            client["connected_bssid"] = "02:00:00:00:02:01"
        elif change == "epoch":
            record["identity"]["epoch"] = "old"
        elif change == "context":
            inspection["bss_loads"][0]["context_state"] = "unverified"
        elif change == "transport":
            record["transport"] = "prpl-1905-broker"
        elif change == "window":
            record["window_seconds"] = 0
        elif change == "fixture":
            record["source"] = "fixture"
        else:
            record["value"] = None
        assert MODULE.native_records(inspection, client, now) == [], change


def test_external_lease_and_active_movement_block_preflight():
    room = {"selected_world": "default", "playback": {"time_ms": 0, "status": "paused"}}
    current = {"health": {"healthy": True}}
    MODULE.preflight(room, current)
    MODULE.preflight({**room, "selected_world": "home-five-agent--private-client-room-walk"}, current)
    MODULE.preflight({**room, "selected_world": "home-five-agent-pods--private-client-room-walk"}, current)
    MODULE.preflight({**room, "selected_world": "home-five-agent-wired--private-client-room-walk"}, current)
    for change in ({"lease": {"held": True}}, {"movement_active": True},
                   {"recording": {"active": True}}, {"selected_world": "rf-asymmetric-ack"}):
        with pytest.raises(RuntimeError):
            MODULE.preflight({**room, **change}, current)


def traffic():
    phase = {"role": "station", "payload_bytes": 256, "mode": "udp", "offered_mbps": 1}
    endpoint = {"status": "complete", "returncode": 0, "bytes": 256, "packets": 1, "seconds": 1,
                "bits_per_second": 2048, "goodput_bits_per_second": 2048}
    result = {"role": "station", "payload_bytes": 256, "world_sha256": "world", "key": ["world", 1, 0],
              "state": "completed", "requested_offered_mbps": 1, "source_interface": "wlan0",
              "sender": deepcopy(endpoint), "receiver": deepcopy(endpoint)}
    return {"golden_sha256": "world", "traffic_experiment": {"phases": [phase]}}, {"state": "off", "history": [result]}


def test_missing_cancelled_old_or_unmeasured_traffic_cannot_pass():
    assert MODULE.traffic_errors(*traffic()) == []
    for change in ("missing", "cancelled", "old", "unmeasured", "cleanup", "not_off"):
        world, sample = traffic()
        if change == "missing":
            sample["history"] = []
        elif change == "cancelled":
            sample["history"][0]["state"] = "cancelled"
        elif change == "old":
            sample["history"][0]["world_sha256"] = "old"
        elif change == "unmeasured":
            sample["history"][0]["receiver"]["status"] = "missing"
        elif change == "cleanup":
            sample["history"][0]["cleanup_errors"] = ["rule still installed"]
        else:
            sample["state"] = "running"
        assert MODULE.traffic_errors(world, sample), change


def test_guest_command_supports_outer_host_without_ssh_and_explicit_remote():
    assert MODULE.guest_command("local", "fixture", "true") == ["lxc", "exec", "fixture", "--", "true"]
    assert MODULE.guest_command("rev140", "demo-a", "true")[-6:] == ["rev140", "lxc", "exec", "demo-a", "--", "true"]


def test_the_results_of_the_labs_composition_of_the_room_count():
    # rdk-1009, 9 October: a lab with a wired extender plays its composition of the golden
    # world (another digest); every phase ran and all were "missing" against the golden file's
    world, sample = traffic()
    sample["history"][0]["world_sha256"] = "composed"
    sample["history"][0]["key"][0] = "composed"
    sample["world_sha256"] = "composed"
    assert MODULE.traffic_errors(world, sample) == []
    # a room that ran no phase still shows the last one's digest: an earlier room's is no result
    assert MODULE.traffic_errors(world, sample, {"composed"}) == ["phase 0: missing traffic result"]
    # nor is another world's result when the service names the room's
    sample["world_sha256"] = "elsewhere"
    assert MODULE.traffic_errors(world, sample) == ["phase 0: missing traffic result"]
