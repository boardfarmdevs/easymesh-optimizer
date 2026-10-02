#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import time
import urllib.request
import uuid


def main():
    parser = argparse.ArgumentParser(description="Bounded room-service crash recovery; native services stay running.")
    parser.add_argument("--flavor", choices=("rdk", "prpl"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host", required=True, help="the host of the lab VM (ssh)")
    parser.add_argument("--vm", required=True, help="the lab VM")
    parser.add_argument("--url", required=True, help="the room service, e.g. http://HOST:PORT")
    parser.add_argument("--yes-act", action="store_true", required=True)
    args = parser.parse_args()
    service = {"rdk": "easymesh-room-service", "prpl": "prplmesh-room-service"}[args.flavor]
    host, machine, url = args.host, args.vm, args.url.rstrip("/")
    recovery_path = f"/run/{service}/recovery.json"
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"flavor": args.flavor, "passed": False, "started": time.time()}

    def save(name, value):
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")

    def guest(command):
        return subprocess.run(["ssh", host, f"lxc exec {machine} -- {command}"],
                              check=True, text=True, capture_output=True, timeout=90).stdout

    def request(path, payload=None, revision=None):
        headers = {"Content-Type": "application/json"}
        if revision is not None:
            headers["If-Match"] = f'"world-revision-{revision}"'
        data = None if payload is None else json.dumps({
            **payload, "command_id": "recovery-smoke-" + uuid.uuid4().hex,
        }).encode()
        with urllib.request.urlopen(urllib.request.Request(url + "/api/demo/" + path,
                                    data=data, headers=headers), timeout=30) as response:
            return json.load(response)

    def ready():
        state = request("interactions")
        current = request("current")
        assert not state["lease"]["held"], "Another operator owns this room"
        health = current.get("health") or {}
        assert health.get("healthy") and health.get("api_total") == 20
        assert state["playback"]["status"] == "paused" and not state["fault"]
        return state, current

    mutated = False
    try:
        state, current = ready()
        previous_run = current["run_id"]
        before = json.loads(guest(f"python3 /tmp/room-feature-guest-audit.py identity {args.flavor}"))
        save("native-before.json", before)
        lease = request("interactions/lease", {"owner": "room-recovery-smoke"})
        mutated = True
        request("world/apply", {"world": "home-a-stationary", "token": lease["token"],
                "expected_revision": state["revision"]}, state["revision"])
        states = []
        for _sample in range(3):
            states.append(request("interactions"))
            time.sleep(0.25)
        save("interactions-before-crash.json", states)
        assert all(state["daemon"] == states[0]["daemon"] for state in states)
        assert all(state["expected_online_clients"] == 10 and not state["fault"] for state in states)
        journal = json.loads(guest(f"cat {recovery_path}"))
        save("recovery-before-crash.json", journal)
        assert journal["last_committed_generation"] == states[-1]["daemon"]["generation"], (
            f"Journal generation {journal['last_committed_generation']} differs from "
            f"room generation {states[-1]['daemon']['generation']}; refusing to crash")
        assert journal["pending_generation"] is None and len(journal["paused_clients"]) == 10
        started = time.monotonic()
        guest(f"systemctl kill --kill-whom=main --signal=SIGKILL {service}")
        deadline = started + 120
        while time.monotonic() < deadline:
            try:
                state, current = ready()
                if current["run_id"] != previous_run:
                    break
            except (OSError, AssertionError):
                pass
            time.sleep(1)
        else:
            raise RuntimeError("Automatic room recovery did not restore the default roster within 120 seconds")
        report["recovery_seconds"] = round(time.monotonic() - started, 3)
        report["recovery_event"] = current["latest"].get("room.recovery.completed")
        assert report["recovery_event"], "No verified automatic recovery event"
        after = json.loads(guest(f"python3 /tmp/room-feature-guest-audit.py identity {args.flavor}"))
        save("native-after.json", after)
        assert before == after, "Native/container/medium identities changed"
        save("final-current.json", current)
        save("final-interactions.json", state)
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)
        if mutated:
            try:
                guest(f"systemctl restart {service}")
            except Exception as cleanup_error:
                report["cleanup_error"] = str(cleanup_error)
        raise
    finally:
        report["finished"] = time.time()
        save("report.json", report)
        print(json.dumps(report))


if __name__ == "__main__":
    main()
