#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from urllib.request import Request, urlopen
import uuid


# The lab this checkout is mounted in (gen/ in the RDK lab, the prplMesh lab's root)
LAB = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("rf_rooms", Path(__file__).with_name("rf-property-rooms-smoke.py"))
ROOMS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ROOMS)
SHUTDOWN_SECONDS = 180
RECOVERY_SECONDS = 180
SERVICE_STOP_SECONDS = 210
SERVICE_START_SECONDS = 30


def restored_journal(path):
    from room_service.recovery import load_recovery

    document = load_recovery(path)
    if (document["state"] != "restored" or document.get("paused_clients")
            or document.get("client_networks")):
        raise RuntimeError("recovery journal still has unrestored RF or client state")
    return {"run_id": document["run_id"], "state": document["state"],
            "paused_clients": [], "client_networks": {}}


def kill_and_reap(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=10)


def recover_manifest(entrypoint, journal, output):
    with (output / "recovery.log").open("w") as log:
        recovery = subprocess.Popen([sys.executable, str(entrypoint), "recover",
            "--recovery-file", str(journal)], stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = recovery.wait(timeout=RECOVERY_SECONDS)
        except BaseException:
            kill_and_reap(recovery)
            raise
    if code != 0:
        raise RuntimeError(f"room-service recover exited {code}; see recovery.log")
    return restored_journal(journal)


def shutdown_manifest(process, entrypoint, journal, output):
    result = {"safe_to_restart": False, "forced_kill": False, "errors": []}
    started = time.monotonic()
    try:
        if process.poll() is None:
            try:
                process.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
        try:
            code = process.wait(timeout=SHUTDOWN_SECONDS)
        except subprocess.TimeoutExpired:
            result["errors"].append(f"manifest shutdown exceeded {SHUTDOWN_SECONDS}s")
            result["forced_kill"] = True
            kill_and_reap(process)
            code = process.returncode
        result["exit_code"] = code
        if code != 0:
            result["errors"].append(f"manifest exited {code}")
        summaries = list((output / "runs").glob("*/interactive-summary.json"))
        try:
            if len(summaries) != 1:
                raise RuntimeError("expected one completed manifest summary")
            summary = json.loads(summaries[0].read_text())
            result["summary"] = {key: summary.get(key) for key in ("run_id", "outcome", "restored", "error")}
            if summary.get("outcome") != "passed" or summary.get("restored") is not True:
                raise RuntimeError("manifest summary did not verify successful restoration")
        except (OSError, ValueError, RuntimeError) as error:
            result["errors"].append(str(error))
        try:
            result["journal"] = restored_journal(journal)
        except Exception as error:
            result["errors"].append(f"journal before recovery: {error}")
            result["recovery_attempted"] = True
            result["journal"] = recover_manifest(entrypoint, journal, output)
        if "summary" in result and result["summary"].get("run_id") != result["journal"]["run_id"]:
            raise RuntimeError("summary/journal run identity mismatch")
        result["safe_to_restart"] = True
    except Exception as error:
        result["errors"].append(f"shutdown/recovery failed: {error}")
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def require_inactive(service):
    state = subprocess.run(["systemctl", "show", service, "--property=ActiveState", "--value"],
                           check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    if state != "inactive":
        raise RuntimeError(f"room service must be inactive, observed {state!r}")


def guard_observation(current, inspection):
    optimizer = current.get("optimizer", {})
    configured = all(row.get("policy_enabled") is True and row.get("counter_guard_enabled") is True
                     for row in (optimizer.get("rf_observations", {}), inspection))
    decisions = [row for row in optimizer.get("client_decisions", []) if row.get("load_evidence") is not None]
    return {"counter_guard_enabled": configured, "observed_load_decisions": decisions}


def fixture_bssid(interfaces, ssid, frequency):
    matches = []
    for block in re.split(r"\bInterface\s+", interfaces)[1:]:
        address = re.search(r"(?m)^\s*addr ([0-9a-f:]{17})\s*$", block)
        name = re.search(r"(?m)^\s*ssid (.+)$", block)
        channel = re.search(r"\((\d+) MHz\)", block)
        if (address and name and channel and name[1].strip() == ssid
                and int(channel[1]) == frequency and re.search(r"(?m)^\s*type AP\s*$", block)):
            matches.append(address[1])
    if len(matches) != 1:
        raise RuntimeError("fixture requires one native AP with the subject's SSID and frequency")
    return matches[0]


def prepare_subject(request, hero, fixture, ap_container):
    from wmdcfg.rf_qualify import association_identity
    from wmdcfg.rf_spatial import roam_client
    from wmdcfg.rf_validate import command

    container = hero["container"]
    original = association_identity(command("lxc", "exec", container, "--", "iw", "dev", "wlan0", "link"))
    if original is None:
        raise RuntimeError("manifest traffic subject has no physical association")
    fixture.update(container=container, original=list(original), verified=False,
                   ap_role="extender_1", ap_container=ap_container,
                   method="explicit pre-play supplicant fixture; not an optimizer action")
    target = fixture_bssid(command("lxc", "exec", ap_container, "--", "iw", "dev"),
                           hero["expected_ssid"], original[1])
    fixture["target_bssid"] = target
    roam_client(container, target, original[1])
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        current = request("current")
        optimizer = current.get("optimizer", {})
        if optimizer.get("mode") != "recommend" or optimizer.get("actions_used", 0) != 0:
            raise RuntimeError("fixture requires recommend mode with zero optimizer actions")
        if any(row.get("role") == hero["role"] and row.get("container") == container
                 and row.get("band") == hero["expected_band"] and row.get("connected_bssid") == target
                 for row in current.get("network", {}).get("clients", [])):
            fixture["verified"] = True
            return
        time.sleep(.5)
    raise RuntimeError("physical/controller traffic fixture did not converge")


def restore_subject(fixture):
    from wmdcfg.rf_spatial import roam_client

    if fixture.get("original"):
        roam_client(fixture["container"], *fixture["original"])
        fixture["restored"] = True


def main():
    parser = argparse.ArgumentParser(description="Bounded opt-in manifest operation; no steering or pressure guarantee")
    parser.add_argument("--stack", choices=("rdk", "prpl"), required=True)
    parser.add_argument("--yes-change-lab", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.yes_change_lab or os.geteuid() != 0 or args.output.exists():
        parser.error("requires root, --yes-change-lab and a new output directory")
    service = "easymesh-room-service" if args.stack == "rdk" else "prplmesh-room-service"
    # the lab's repository: its manifests name their files from it
    root = LAB.parent if args.stack == "rdk" else LAB
    prefix = LAB
    for directory in ("rooms", "optimizer", "medium/configurator"):
        sys.path.insert(0, str(prefix / directory))
    entrypoint = prefix / "rooms/room-service"
    journal = args.output.resolve() / "manifest-recovery.json"
    default_journal = Path("/run") / service / "recovery.json"
    report = {"passed": False, "restored_default": False, "samples": [], "scope": "named manifest/recommend/no actuation"}
    process = None
    stopped = False
    token = None
    baseline_restored = False

    def request(suffix, body=None, revision=None):
        headers = {"Content-Type": "application/json"}
        if revision is not None:
            headers["If-Match"] = f'"world-revision-{revision}"'
        if body is not None:
            body = {**body, "command_id": "counter-manifest-" + uuid.uuid4().hex}
        with urlopen(Request("http://127.0.0.1:8891/api/demo/" + suffix,
                             data=None if body is None else json.dumps(body).encode(), headers=headers), timeout=10) as response:
            return json.load(response)

    def healthy(count, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                current = request("current")
                if current.get("health", {}).get("healthy") and len(current.get("network", {}).get("clients", [])) == count:
                    return current
            except OSError:
                pass
            if process is not None and process.poll() is not None:
                raise RuntimeError("manifest process exited before readiness")
            time.sleep(1)
        raise RuntimeError(f"{count}-client readiness timeout")

    if Path("/run/easymesh-suite-room-guard").exists():
        raise RuntimeError("external suite owns room")
    original = request("interactions")
    ROOMS.preflight(original, request("current"))
    args.output.mkdir(parents=True)
    try:
        stopped = True
        subprocess.run(["systemctl", "stop", service], check=True, timeout=SERVICE_STOP_SECONDS)
        require_inactive(service)
        report["baseline_journal"] = restored_journal(default_journal)
        baseline_restored = True
        with (args.output / "manifest.log").open("w") as log:
            process = subprocess.Popen([sys.executable, str(entrypoint), "interactive",
                "--mode", "recommend", "--profiling", "--manifest",
                str(prefix / "rooms/manifests/native-counter-guard-room-profile.json"),
                "--listen", "0.0.0.0:8891", "--serve-seconds", "130", "--output-root", str(args.output / "runs"),
                "--recovery-file", str(journal)], cwd=root, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True)
            report["initial"] = healthy(10, 60)
            report["traffic_fixture"] = {}
            manifest = json.loads((prefix / "rooms/manifests/native-counter-guard-room-profile.json").read_text())
            bindings = json.loads((root / manifest["bindings"]).read_text())["roles"]
            prepare_subject(request, manifest["hero"], report["traffic_fixture"], bindings["extender_1"])
            token = request("interactions/lease", {"owner": "counter-guard-manifest-smoke"})["token"]
            room = request("interactions")
            request("playback", {"token": token, "action": "play"}, room["revision"])
            deadline = time.monotonic() + 40
            enabled = False
            report["observed_load_decisions"] = []
            while time.monotonic() < deadline:
                request("interactions/lease", {"token": token})
                current = request("current")
                room = request("interactions")
                inspection = request("rf-observations")
                report["samples"].append({"current": current, "room": room, "inspection": inspection})
                optimizer = current.get("optimizer", {})
                if optimizer.get("mode") != "recommend" or optimizer.get("actions_used", 0) != 0:
                    raise RuntimeError("manifest must remain recommend-only with zero actions")
                observation = guard_observation(current, inspection)
                enabled = observation["counter_guard_enabled"]
                report["observed_load_decisions"].extend(observation["observed_load_decisions"])
                if room["playback"]["status"] == "completed":
                    break
                time.sleep(2)
            else:
                raise RuntimeError("manifest playback timeout")
            world = json.loads((prefix / "medium/configurator/worlds/golden/rf-asymmetric-ack.world.json").read_text())
            errors = ROOMS.traffic_errors(world, room["traffic_experiment"])
            report["counter_guard_enabled"] = enabled
            report["guard_enabled_in_decisions"] = any(
                row["load_evidence"].get("counter_guard_enabled") is True for row in report["observed_load_decisions"])
            report["traffic_errors"] = errors
            if not enabled or errors:
                raise RuntimeError(f"guard enabled in RF inspectors={enabled}; traffic errors={errors}")
            report["passed"] = True
    except (Exception, KeyboardInterrupt) as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        report["checks_passed"] = report["passed"]
        report["passed"] = False
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        if process is not None:
            report["shutdown"] = shutdown_manifest(process, entrypoint, journal, args.output)
            baseline_restored = report["shutdown"]["safe_to_restart"]
            if report["shutdown"]["errors"]:
                report["stop_error"] = "; ".join(report["shutdown"]["errors"])
            process = None
        try:
            if stopped:
                if not baseline_restored:
                    raise RuntimeError("baseline recovery unverified; Default service left stopped for operator recovery")
                restore_subject(report.get("traffic_fixture", {}))
                if Path("/run/easymesh-suite-room-guard").exists():
                    raise RuntimeError("external suite acquired room during cleanup")
                require_inactive(service)
                subprocess.run(["systemctl", "start", service], check=True, timeout=SERVICE_START_SECONDS)
                report["restored_current"] = healthy(20, 60)
                ROOMS.preflight(request("interactions"), report["restored_current"])
                report["restored_default"] = True
        except Exception as error:
            report["restore_error"] = str(error)
        report["passed"] = (report["checks_passed"] and report["restored_default"]
                            and not any(key.endswith("error") for key in report))
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key not in {"samples", "initial", "restored_current"}}))
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
