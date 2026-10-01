#!/usr/bin/env python3
"""Fixed-pool world switching in a lab VM (run in the guest): every room applied, its exact
roster, health and optimizer convergence awaited, the default room restored in the end, and
no container, service or medium process restarted. One tool for both stacks (--stack)."""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid

# The default room of each room set (worlds, worlds-wired, worlds-pods).
DEFAULT_WORLD = re.compile(r"home-five-agent(?:-(wired|pods))?--private-client-room-walk")

# What the stacks differ in: their containers, the processes that must not restart, the
# controller's topology API and how many topology nodes the mesh devices make.
STACKS = {
    "rdk": {
        "clients": "wlan-client", "aps": ("bpiap", "bpibroadband"), "controller": "bpibroadband",
        "controller_url": "http://127.0.0.1:8888", "topology": "/api/v1/topology",
        # the controller is a topology node of its own, next to the gateway's agent
        "topology_nodes_beyond_mesh": 1,
    },
    "prpl": {
        "clients": "prpl-client-", "aps": ("prpl-agent-", "prpl-controller"), "controller": None,
        "controller_url": "http://127.0.0.1:8092", "topology": "/api/topology",
        "topology_nodes_beyond_mesh": 0,
    },
}


def command(*arguments):
    return subprocess.run(arguments, check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def detect_stack(names):
    if "bpibroadband" in names:
        return "rdk"
    if "prpl-controller" in names:
        return "prpl"
    raise RuntimeError("neither an RDK (bpibroadband) nor a prplMesh (prpl-controller) lab here")


def ap_services(stack, name):
    if stack == "rdk":
        return command("lxc", "exec", name, "--", "systemctl", "show",
                       "onewifi", "em_agent", "-p", "MainPID", "-p", "NRestarts", "-p", "ActiveState")
    return command("lxc", "exec", name, "--", "pgrep", "-a", "-f", "beerocks_|ieee1905_transport|hostapd -B")


def medium_process(stack):
    if stack == "rdk":
        medium_pid = Path("/run/meta-cmf-wmediumd/wmediumd.pid").read_text().strip()
        return command("ps", "-p", medium_pid, "-o", "pid=,lstart=,args=")
    return command("pgrep", "-a", "-x", "wmediumd")


def identities(stack):
    profile = STACKS[stack]
    instances = json.loads(command("lxc", "query", "/1.0/instances?recursion=2"))
    result = {}
    for instance in instances:
        name = instance["name"]
        if not name.startswith((profile["clients"], *profile["aps"])):
            continue
        state = instance["state"]
        if state["status"] != "Running":
            raise RuntimeError(f"{name} is not running")
        result[name] = state["pid"]
        if not name.startswith(profile["clients"]):
            result[name + "/services"] = ap_services(stack, name)
    if sum(name.startswith(profile["clients"]) for name in result) != 100:
        raise RuntimeError("requires the provisioned 100-client pool")
    if profile["controller"]:
        result["controller-services"] = command("lxc", "exec", profile["controller"], "--", "systemctl", "show",
            "em_ctrl", "em_cli", "-p", "MainPID", "-p", "NRestarts", "-p", "ActiveState")
    result["medium-process"] = medium_process(stack)
    return result


def topology_stations(stack, topology):
    """The controller's associated clients and its topology node count."""
    if stack == "rdk":
        return ({station["staMAC"].lower() for node in topology["nodes"]
                 for station in node.get("STAList", []) or []}, len(topology["nodes"]))
    return ({station["id"].lower() for device in topology["devices"]
             for radio in device.get("radios", []) for bss in radio.get("bsses", [])
             for station in bss.get("clients", [])}, len(topology["devices"]))


def client_convergence(current, expected):
    optimizer = current.get("optimizer", {})
    fleet = optimizer.get("fleet", {})
    rows = optimizer.get("client_decisions", [])
    clients = {client["sta_mac"]: client for client in current.get("network", {}).get("clients", [])}
    identities_match = (len(rows) == len(clients) == expected
                        and {row.get("sta_mac") for row in rows} == set(clients)
                        and all(row.get("current_rcpi") is not None
                                and row.get("source_bssid") == clients[row["sta_mac"]].get("connected_bssid")
                                for row in rows))
    policy_checked = identities_match and fleet.get("converged") is True
    absolute_checked = identities_match and fleet.get("absolute_best_converged") is True and all(
        all(score.get("gain_rcpi", 0) <= 0 for score in row.get("scores", [])
            if score.get("band") == row.get("current_band")) for row in rows)
    return policy_checked, absolute_checked


def release_control(request_fn, token):
    return request_fn("/api/demo/interactions/lease", {"token": token}, method="DELETE")


def main():
    parser = argparse.ArgumentParser(description="Opt-in fixed-pool world switching acceptance; restores default in finally")
    parser.add_argument("--yes-act", action="store_true")
    parser.add_argument("--stack", choices=sorted(STACKS), help="the lab's stack (default: from its containers)")
    parser.add_argument("--base-url", default="http://127.0.0.1:8891")
    parser.add_argument("--controller-url", help="the controller's topology API (default: the stack's)")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--all-worlds", action="store_true", help="test every compatible installed room")
    parser.add_argument("--world", action="append", help="test only these room IDs, then restore default")
    parser.add_argument("--skip-presence", action="store_true", help="omit the additional disappear/reappear cycle")
    parser.add_argument("--require-absolute-best", action="store_true",
                        help="also require no stronger same-band AP, beyond the configured steering policy")
    parser.add_argument("--roster-only", action="store_true",
                        help="check membership and health without claiming optimizer convergence")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.yes_act:
        parser.error("--yes-act is required: this changes live room RF")
    if args.all_worlds and args.world:
        parser.error("choose --all-worlds or --world, not both")
    stack = args.stack or detect_stack(
        {instance["name"] for instance in json.loads(command("lxc", "query", "/1.0/instances?recursion=1"))})
    profile = STACKS[stack]
    controller_url = args.controller_url or profile["controller_url"]
    lease = None
    lease_renewed = 0.0
    report = {"worlds": [], "restored_default": False, "passed": False}

    def request(path, body=None, *, controller=False, revision=None, method=None):
        headers = {"Content-Type": "application/json"}
        if body is not None:
            body = {**body, "command_id": "smoke-" + uuid.uuid4().hex}
        if revision is not None:
            headers["If-Match"] = f'"world-revision-{revision}"'
        target = (controller_url if controller else args.base_url) + path
        query = urllib.request.Request(target, data=None if body is None else json.dumps(body).encode(), headers=headers, method=method)
        try:
            with urllib.request.urlopen(query, timeout=120 if body is not None or path == "/api/demo/interactions" else 8) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if controller and error.code >= 500:
                raise urllib.error.URLError(f"controller HTTP {error.code}") from None
            detail = json.load(error)
            raise RuntimeError(f"{path}: {detail.get('error')}: {detail.get('message')}") from None

    def renew():
        nonlocal lease_renewed
        if time.monotonic() - lease_renewed >= 8:
            request("/api/demo/interactions/lease", {"token": lease})
            lease_renewed = time.monotonic()

    def apply(name):
        renew()
        snapshot = request("/api/demo/interactions")
        result = request("/api/demo/world/apply", {"token": lease, "world": name}, revision=snapshot["revision"])
        print(json.dumps({"applied": name, "target": result["expected_online_clients"],
                          "generation": result["daemon_generation"]}), flush=True)
        return result

    # one topology node per mesh device the room binds (six for the lab's own rooms; OpenSync
    # pods add theirs), and on RDK the controller's own
    mesh_nodes = request("/api/demo/worlds").get("mesh_devices", 5) + profile["topology_nodes_beyond_mesh"]

    def wait_for_world(name, expected, require_convergence=True):
        started = time.monotonic()
        stable_since = None
        while time.monotonic() - started < args.timeout:
            renew()
            try:
                current = request("/api/demo/current")
                topology = request(profile["topology"], controller=True)
            except (TimeoutError, urllib.error.URLError) as error:
                stable_since = None
                print(json.dumps({"waiting": name, "telemetry_unavailable": str(error)}), flush=True)
                time.sleep(2)
                continue
            actual, topology_nodes = topology_stations(stack, topology)
            desired = {mac for role, mac in mac_by_role.items() if current["roles"].get(role, {}).get("present")}
            health = current.get("health", {})
            optimizer = current.get("optimizer", {})
            fleet = optimizer.get("fleet", {})
            rows = optimizer.get("client_decisions", [])
            policy_checked, best_ap_checked = client_convergence(current, expected)
            evaluated_at = optimizer.get("evaluated_at")
            evaluation_age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(
                evaluated_at.replace("Z", "+00:00"))).total_seconds() if evaluated_at else float("inf")
            mesh = current.get("network", {}).get("mesh", {})
            control = mesh.get("backhaul_control", {})
            reported_parents = {edge["child_role"]: edge["parent_role"] for edge in mesh.get("backhaul_edges", [])}
            backhaul_settled = not control or (
                control.get("status") == "stable" and control.get("parents") == reported_parents
                and len(reported_parents) == 4
            )
            converged = (optimizer.get("expected_online_clients") == expected
                         and optimizer.get("environment_epoch") == current.get("environment_epoch")
                         and fleet.get("converged") is True
                         and fleet.get("measurement_complete") is True
                         and fleet.get("clients_checked") == expected
                         and fleet.get("clients_evaluated") == expected
                         and policy_checked and (best_ap_checked or not args.require_absolute_best)
                         and -5 <= evaluation_age <= 60 and backhaul_settled)
            if (actual == desired and len(actual) == expected and topology_nodes == mesh_nodes
                    and health.get("healthy") is True and health.get("expected_online_clients") == expected
                    and (converged or not require_convergence)):
                offline = [role for role in mac_by_role if not current["roles"][role]["present"]]
                def verify_offline(role):
                    link = command("lxc", "exec", containers_by_role[role], "--", "iw", "dev", "wlan0", "link")
                    if "Not connected" not in link:
                        raise RuntimeError(f"{role}: controller roster converged but isolated client remains connected")

                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as workers:
                    list(workers.map(verify_offline, offline))
                if stable_since is None:
                    stable_since = time.monotonic()
                if time.monotonic() - stable_since < 10:
                    time.sleep(3)
                    continue
                result = {"world": name, "clients": len(actual), "mesh_nodes": topology_nodes,
                          "elapsed_seconds": round(time.monotonic() - started, 2), "healthy": True,
                          "kernel_offline_clients_verified": len(offline),
                          "passed": True, "fleet_converged": converged,
                          "configured_policy_verified": policy_checked,
                          "best_eligible_same_band_ap_verified": best_ap_checked,
                          "backhaul_settled": backhaul_settled, "backhaul_parents": reported_parents,
                          "evaluation_age_seconds": round(evaluation_age, 3),
                          "client_decisions": rows,
                          "clients_checked": fleet.get("clients_checked"),
                          "actions_used": optimizer.get("actions_used"),
                          "kernel_current_link_measurements": optimizer.get("kernel_current_link_measurements", []),
                          "associations": [{"sta_mac": client["sta_mac"],
                                            "ap": client["connected_device_name"],
                                            "bssid": client["connected_bssid"],
                                            "rssi_dbm": client["rssi_dbm"]}
                                           for client in current.get("network", {}).get("clients", [])]}
                print(json.dumps(result), flush=True)
                return result
            stable_since = None
            print(json.dumps({"waiting": name, "actual": len(actual), "expected": expected,
                              "healthy": health.get("healthy"), "fleet_converged": converged,
                              "stronger_ap_clients": fleet.get("clients_with_stronger_ap"),
                              "best_ap_checked": best_ap_checked, "backhaul_settled": backhaul_settled,
                              "decision": optimizer.get("decision", {}).get("reason"),
                              "actions_used": optimizer.get("actions_used"),
                              "elapsed": round(time.monotonic() - started)}), flush=True)
            time.sleep(3)
        raise RuntimeError(f"{name}: exact {expected}-client roster and measured policy convergence not achieved")

    report["stack"] = stack
    before = identities(stack)
    preflight_deadline = time.monotonic() + args.timeout
    while True:
        current = request("/api/demo/current")
        if not DEFAULT_WORLD.fullmatch(current.get("scenario") or ""):
            raise RuntimeError("start acceptance from the default room")
        mac_by_role = {client["role"]: client["sta_mac"].lower() for client in current.get("network", {}).get("clients", [])}
        containers_by_role = {client["role"]: client["container"] for client in current.get("network", {}).get("clients", [])}
        if len(mac_by_role) == 20 and current.get("health", {}).get("healthy") is True:
            break
        if time.monotonic() >= preflight_deadline:
            raise RuntimeError("start acceptance from a healthy default 20-client room")
        time.sleep(2)
    pool = request("/api/demo/worlds").get("client_bindings", {})
    if len(pool) != 100 or any(pool.get(role, {}).get("sta_mac", "").lower() != mac
                               for role, mac in mac_by_role.items()):
        raise RuntimeError("catalog identities must cover the full pool and match the default room")
    mac_by_role = {role: client["sta_mac"].lower() for role, client in pool.items()}
    containers_by_role = {role: client["container"] for role, client in pool.items()}
    report["run_id"] = current["run_id"]
    original_room = request("/api/demo/interactions")
    if original_room.get("movement_active") or original_room.get("recording", {}).get("active"):
        raise RuntimeError("pause movement and recording before acceptance")
    if original_room.get("playback", {}).get("time_ms") != 0 or original_room.get("lease", {}).get("held"):
        raise RuntimeError("requires paused initial playback and no existing control lease")
    report["original_room"] = original_room
    try:
        lease = request("/api/demo/interactions/lease", {"owner": "world-switch-acceptance"})["token"]
        lease_renewed = time.monotonic()
        names = args.world or ([world["id"] for world in request("/api/demo/worlds")["worlds"]] if args.all_worlds else
                 ["home-a-border-hover", "home-a-stationary", "home-b-slow-walk-ten", "home-a-flash-crowd"])
        for name in [*names, "default"]:
            expected = apply(name)["expected_online_clients"]
            try:
                result = wait_for_world(name, expected, require_convergence=not args.roster_only)
            except RuntimeError as error:
                if not args.all_worlds:
                    raise
                current = request("/api/demo/current")
                if current.get("error") or current.get("state") != "running":
                    raise
                result = {"world": name, "expected_clients": expected, "passed": False,
                          "error": str(error), "health": current.get("health"),
                          "optimizer": current.get("optimizer")}
            report["worlds"].append(result)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
        for present, expected in (() if args.skip_presence else ((False, 19), (True, 20))):
            renew()
            snapshot = request("/api/demo/interactions")
            request("/api/demo/roles/sta_mobile_01/presence", {"token": lease, "present": present},
                    revision=snapshot["revision"], method="PUT")
            report["worlds"].append(wait_for_world("client-reappear" if present else "client-disappear", expected,
                                                  require_convergence=not args.roster_only))
        report["containers_and_services_unchanged"] = identities(stack) == before
        if not report["containers_and_services_unchanged"]:
            raise RuntimeError("container, service or medium process identity changed")
        report["passed"] = all(world.get("passed") for world in report["worlds"])
        if not report["passed"]:
            raise RuntimeError("one or more rooms failed measured convergence; see per-room results")
    except Exception as error:
        report["error"] = str(error)
        print(json.dumps({"failed": str(error)}), flush=True)
        raise
    finally:
        try:
            if lease is not None:
                if not request("/api/demo/interactions").get("lease", {}).get("held"):
                    lease = request("/api/demo/interactions/lease", {"owner": "world-switch-acceptance"})["token"]
                    lease_renewed = time.monotonic()
                apply("default")
                for role, saved in original_room["roles"].items():
                    snapshot = request("/api/demo/interactions")
                    if snapshot["roles"][role]["position"] != saved["position"]:
                        renew()
                        request("/api/demo/roles/" + role + "/position",
                                {"token": lease, "position": saved["position"], "final": True},
                                revision=snapshot["revision"], method="PUT")
                report["final_restoration"] = wait_for_world("default-restore", 20,
                                                             require_convergence=not args.roster_only)
                report["restored_default"] = True
        except Exception as error:
            report["restore_error"] = str(error)
            report["passed"] = False
            print(json.dumps({"restore_failed": str(error)}), flush=True)
        finally:
            if lease is not None:
                try:
                    release_control(request, lease)
                except Exception as error:
                    report["lease_release_error"] = str(error)
                    report["passed"] = False
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            if report.get("lease_release_error"):
                raise RuntimeError("acceptance did not release its control lease: " + report["lease_release_error"])
        if lease is not None and not report["restored_default"]:
            raise RuntimeError("acceptance did not restore the default room")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
