#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.request import Request, urlopen
import uuid


# The lab this checkout is mounted in (gen/ in the RDK lab, the prplMesh lab's root)
LAB = Path(__file__).resolve().parents[2]


def _worlds():
    """The Golden Worlds the room runs: the medium's own, or a variant the suite names
    (EASYMESH_ROOM_WORLDS_ROOT, relative to the lab's repository as the suites give it)."""
    chosen = os.environ.get("EASYMESH_ROOM_WORLDS_ROOT")
    if not chosen:
        return LAB / "medium/configurator/worlds"
    candidates = [Path(chosen)] if Path(chosen).is_absolute() else [LAB.parent / chosen, LAB / chosen]
    return next((path for path in candidates if path.is_dir()), candidates[0])


WORLDS = _worlds()
ROOMS = ("rf-packet-size-counters", "rf-asymmetric-ack")
# The default room of each room set (worlds, worlds-wired, worlds-pods).
DEFAULT_WORLD = re.compile(r"home-five-agent(?:-(wired|pods))?--private-client-room-walk")
COUNTERS = {"packets_per_second", "bytes_per_second", "retries_per_second",
            "tx_errors_per_second", "rx_errors_per_second"}


def guest_command(host, vm, *arguments):
    command = ["lxc", "exec", vm, "--", *arguments]
    return command if host == "local" else ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, *command]


def native_records(inspection, client, now):
    if not client or not inspection.get("enabled") or inspection.get("error"):
        return []
    envelope = inspection.get("observations", {})
    if envelope.get("decision_inputs") is not False:
        return []
    current = [row for row in inspection.get("bss_loads", [])
               if row.get("bssid") == client.get("connected_bssid")
               and row.get("context_state") == "verified" and row.get("source") == "native_ap_metrics"]
    if len(current) != 1:
        return []
    load = current[0]
    result = []
    for row in envelope.get("records", []):
        identity = row.get("identity", {})
        activity = row.get("property") in COUNTERS
        if row.get("property") not in COUNTERS | {"native_utilization", "station_count"}:
            continue
        try:
            age = now - datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00")).timestamp()
            valid = (row["state"] == "valid" and type(row["value"]) in (int, float)
                     and math.isfinite(row["value"]) and row["value"] >= 0 and 0 <= age <= 5
                     and identity.get("bssid") == client["connected_bssid"]
                     and identity.get("epoch") == load["epoch"]
                     and row.get("source") == ("native_sta_traffic" if activity else "native_ap_metrics")
                     and row.get("transport") == load.get("transport"))
            if activity:
                valid = valid and identity.get("sta_mac") == client["sta_mac"] and 0 < row["window_seconds"] <= 10
            else:
                valid = valid and identity.get("context_state") == "verified"
            if valid:
                result.append(row)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return result


def traffic_errors(world, traffic, earlier=()):
    """The room's traffic phases (``world``'s) against the results of the world the room service
    played: its digest is the service's (``traffic["world_sha256"]``), which on a lab with a
    wired extender or pods is the lab's composition of the golden world (rdk-1009, 9 October:
    every phase ran, and all were "missing" against the golden file's digest). The state keeps
    the last phase's digest: one of an ``earlier`` room's means this room ran none."""
    errors = []
    if traffic.get("state") != "off":
        errors.append("experimental traffic did not stop")
    played = traffic.get("world_sha256")
    if not played or played in earlier:
        played = world["golden_sha256"]
    for index, phase in enumerate(world["traffic_experiment"]["phases"]):
        rows = [row for row in traffic.get("history", [])
                if row.get("world_sha256") == played
                and row.get("key", [None, None, None])[2] == index]
        if not rows:
            errors.append(f"phase {index}: missing traffic result")
            continue
        row = rows[-1]
        if (row.get("role") != phase["role"] or row.get("payload_bytes") != phase["payload_bytes"]
                or row.get("state") != "completed" or row.get("cleanup_errors")):
            errors.append(f"phase {index}: failed/cancelled/mismatched traffic result")
            continue
        if phase.get("mode") == "udp":
            sender, receiver = row.get("sender", {}), row.get("receiver", {})
            valid = (row.get("requested_offered_mbps") == phase["offered_mbps"]
                     and row.get("source_interface") == "wlan0")
            for endpoint, rate in ((sender, "bits_per_second"), (receiver, "goodput_bits_per_second")):
                valid = valid and (endpoint.get("status") == "complete" and endpoint.get("returncode") == 0
                                   and all(type(endpoint.get(field)) in (int, float)
                                           and math.isfinite(endpoint[field]) and endpoint[field] > 0
                                           for field in ("bytes", "packets", "seconds", rate)))
            if not valid:
                errors.append(f"phase {index}: unavailable sender/receiver measurements")
        elif (row.get("requested_packets_per_second") != phase["packets_per_second"]
              or not 0 < (row.get("received_echo_replies") or 0) <= (row.get("transmitted_packets") or 0)):
            errors.append(f"phase {index}: no measured echo delivery")
    return errors


def preflight(room, current):
    if (room.get("lease", {}).get("held") or room.get("movement_active")
            or room.get("recording", {}).get("active")):
        raise RuntimeError("external owner/movement/recording active; leave the room untouched")
    if ((room.get("selected_world") != "default" and not DEFAULT_WORLD.fullmatch(room.get("selected_world") or ""))
            or room.get("playback", {}).get("time_ms") != 0
            or room.get("playback", {}).get("status") != "paused"
            or current.get("health", {}).get("healthy") is not True):
        raise RuntimeError("requires healthy paused Default at time zero")


def main():
    parser = argparse.ArgumentParser(description="Bounded new RF-room checks with lease and Default restoration")
    parser.add_argument("--yes-act", action="store_true")
    parser.add_argument("--room-url", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--vm", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.yes_act:
        parser.error("--yes-act is required")
    if args.output.exists():
        parser.error("choose a new output to preserve evidence")
    if not all(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.@-]*", value) for value in (args.host, args.vm)):
        parser.error("invalid host/VM name")
    report = {"passed": False, "rooms": [], "restored_default": False,
              "scope": "room playback/native observation/traffic; no positive load-steer or optional-mode qualification"}
    token = None
    renewed = 0

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")

    def request(suffix, body=None, *, revision=None, method=None):
        headers = {"Content-Type": "application/json"}
        if body is not None:
            body = {**body, "command_id": "rf-properties-" + uuid.uuid4().hex}
        if revision is not None:
            headers["If-Match"] = f'"world-revision-{revision}"'
        query = Request(args.room_url.rstrip("/") + "/api/demo/" + suffix,
                        data=None if body is None else json.dumps(body).encode(), headers=headers, method=method)
        with urlopen(query, timeout=45 if body is not None else 5) as response:
            return json.load(response)

    def renew():
        nonlocal renewed
        if time.monotonic() - renewed >= 6:
            request("interactions/lease", {"token": token})
            renewed = time.monotonic()

    def mutate(suffix, values):
        renew()
        state = request("interactions")
        return request(suffix, {"token": token, **values}, revision=state["revision"])

    def settle(expected, seconds):
        deadline = time.monotonic() + seconds
        check = {"expected_clients": expected, "deadline_seconds": seconds, "samples": []}
        report.setdefault("settle_checks", []).append(check)
        while time.monotonic() < deadline:
            renew()
            current = request("current")
            check["samples"].append({
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "health": {key: value for key, value in current.get("health", {}).items()
                           if key != "evidence_storage"},
                "clients": [{key: row.get(key) for key in ("role", "sta_mac", "connected_bssid")}
                            for row in current.get("network", {}).get("clients", [])],
            })
            if (current.get("health", {}).get("healthy") is True
                    and current["health"].get("expected_online_clients") == expected
                    and len(current.get("network", {}).get("clients", [])) == expected):
                return current
            time.sleep(1)
        raise RuntimeError(f"native {expected}-client roster/health did not settle within {seconds}s")

    try:
        subprocess.run(guest_command(args.host, args.vm, "test", "!", "-e", "/run/easymesh-suite-room-guard"),
                       check=True, capture_output=True, text=True, timeout=10)
        original = request("interactions")
        preflight(original, request("current"))
        report["original_room"] = original
        report["catalog"] = request("rf-catalog")
        token = request("interactions/lease", {"owner": "rf-property-rooms-smoke"})["token"]
        renewed = time.monotonic()
        played_before = set()
        for name in ROOMS:
            result = {"room": name, "passed": False, "samples": [], "errors": []}
            report["rooms"].append(result)
            world = json.loads((WORLDS / "golden" / f"{name}.world.json").read_text())
            result["world_sha256"] = world["golden_sha256"]
            result["applied"] = mutate("world/apply", {"world": name})
            result["initial"] = settle(10, 60)
            mutate("playback", {"action": "play"})
            deadline = time.monotonic() + 50
            properties = set()
            while time.monotonic() < deadline:
                renew()
                room = request("interactions")
                current = request("current")
                inspection = request("rf-observations")
                client = next((row for row in current.get("network", {}).get("clients", [])
                               if row.get("role") == "sta_static_03"), None)
                observed_at = datetime.now(timezone.utc)
                records = native_records(inspection, client, observed_at.timestamp())
                properties.update(row["property"] for row in records)
                result["samples"].append({"observed_at": observed_at.isoformat(), "playback": room["playback"],
                    "client": client, "records": records, "receiver_error": inspection.get("error"),
                    "context_state": inspection.get("context_state"), "traffic": room["traffic_experiment"],
                    "decisions": current.get("optimizer", {}).get("client_decisions", [])})
                if room["playback"]["status"] == "completed":
                    break
                time.sleep(2)
            else:
                result["errors"].append("playback exceeded 50s deadline")
            result["observed_properties"] = sorted(properties)
            result["errors"] += ["no fresh owned native " + name for name in sorted(
                (COUNTERS | {"native_utilization", "station_count"}) - properties)]
            # the room service plays the room, or the lab's composition of it (LAYOUT--ROOM)
            selected = room.get("selected_world") or ""
            if selected != name and not selected.endswith("--" + name):
                result["errors"].append(f"the room service played {selected or 'nothing'}, not {name}")
            result["played_world"] = selected
            result["played_world_sha256"] = room["traffic_experiment"].get("world_sha256")
            result["errors"] += traffic_errors(world, room["traffic_experiment"], played_before)
            played_before.add(result["played_world_sha256"])
            result["passed"] = not result["errors"]
            save()
            print(json.dumps({"room": name, "passed": result["passed"], "errors": result["errors"],
                              "observed_properties": result["observed_properties"]}), flush=True)
    except Exception as error:
        report["error"] = str(error)
        print(json.dumps({"failed": str(error)}), flush=True)
    finally:
        if token is not None:
            try:
                mutate("world/apply", {"world": "default"})
                report["restored_current"] = settle(20, 60)
                restored = request("interactions")
                if (restored["selected_world"] != original["selected_world"] or restored["roles"] != original["roles"]
                        or restored["playback"]["status"] != "paused" or restored["playback"]["time_ms"] != 0
                        or restored["traffic_experiment"]["state"] not in {"off", "idle"}):
                    raise RuntimeError("Default RF/presence/playback/traffic restoration mismatch")
                report["restored_default"] = True
            except Exception as error:
                report["restore_error"] = str(error)
            finally:
                try:
                    request("interactions/lease", {"token": token}, method="DELETE")
                except Exception as error:
                    report["release_error"] = str(error)
        report["passed"] = (len(report["rooms"]) == len(ROOMS) and all(row["passed"] for row in report["rooms"])
                            and report["restored_default"] and not any(key in report for key in
                                                                       ("error", "restore_error", "release_error")))
        save()
    print(json.dumps({key: value for key, value in report.items()
                      if key in {"passed", "restored_default", "error", "restore_error", "release_error"}}), flush=True)
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
