#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path
import re
import time
import urllib.request


def parse_timestamp(value):
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp is missing or is not a string")
    return dt.datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00"))


def freshness_state(optimizer, clients, now):
    errors = []

    def age(value, field):
        try:
            return (now - parse_timestamp(value)).total_seconds()
        except (ValueError, TypeError) as error:
            errors.append({"field": field, "error": str(error)})
            return None

    ages = [age(optimizer.get("evaluated_at"), "optimizer.evaluated_at")]
    ages.extend(age(client.get("metric_observed_at"),
                    f"client[{client.get('sta_mac', 'unknown')}].metric_observed_at")
                for client in clients)
    fresh = len(clients) == 20 and all(value is not None and 0 <= value <= 30 for value in ages) and all(
        isinstance(client.get("rcpi"), (int, float)) and not isinstance(client["rcpi"], bool)
        and math.isfinite(client["rcpi"]) and client["rcpi"] > 0 for client in clients)
    return {"fresh": fresh, "timestamp_errors": errors}


def convergence_state(optimizer, clients):
    fleet = optimizer.get("fleet") or {}
    decisions = optimizer.get("client_decisions") or []
    identity = lambda value: value.lower() if isinstance(value, str) else ""
    current = {identity(client.get("sta_mac")): client for client in clients}
    coverage = bool(clients) and all(current) and len(current) == len(clients) == len(decisions) and (
        sorted(identity(row.get("sta_mac")) for row in decisions) == sorted(current)) and all(
        identity(row.get("source_bssid")) and
        identity(row["source_bssid"]) == identity(current[identity(row.get("sta_mac"))].get("connected_bssid")) and
        isinstance(row.get("current_rcpi"), (int, float)) and not isinstance(row["current_rcpi"], bool) and
        math.isfinite(row["current_rcpi"]) for row in decisions)
    same_band_best = coverage and all(all(
        isinstance(score.get("gain_rcpi"), (int, float)) and not isinstance(score["gain_rcpi"], bool) and
        math.isfinite(score["gain_rcpi"]) and score["gain_rcpi"] <= 0
        for score in row.get("scores", []) if score.get("band") == row.get("current_band"))
        for row in decisions)
    return {
        "decision_coverage": bool(coverage),
        "policy_converged": bool(coverage) and fleet.get("converged") is True,
        "absolute_best_converged": bool(same_band_best) and fleet.get("converged") is True and fleet.get("clients_with_stronger_ap") == 0,
        "stronger_candidates": fleet.get("stronger_candidates", []),
    }


# The default room of each room set: the lab's own rooms (worlds), the standard rooms
# with the wired extender (worlds-wired), or those with the OpenSync pods as well
# (worlds-pods). The same world, the set's suffix on its layout.
DEFAULT_WORLD = re.compile(r"home-five-agent(?:-(wired|pods))?--private-client-room-walk")


def default_roster(health, clients, state):
    stations = {f"sta_{kind}_{ordinal:02d}" for kind in ("mobile", "static")
                for ordinal in range(1, 11)}
    mesh = {"gateway", *(f"extender_{ordinal}" for ordinal in range(1, 5))}
    dormant = {f"sta_pool_{ordinal:03d}" for ordinal in range(21, 101)}
    roles = state.get("roles") or {}
    # OpenSync pods through the EMOSA adapter (the pod room variant) are mesh roles too
    mesh |= {role for role in roles if re.fullmatch(r"pod_\d+", role)}
    world = state.get("selected_world")
    variant = DEFAULT_WORLD.fullmatch(world or "")
    if variant and variant.group(1) in ("wired", "pods"):
        mesh.add("extender_5")  # the lab's extender on a wired backhaul
    present = {role for role, value in roles.items() if value.get("present") is True}
    addresses = [client.get("sta_mac") for client in clients]
    return (health.get("api_total") == 20 and len(clients) == 20
            and all(isinstance(address, str) and address for address in addresses)
            and len({address.lower() for address in addresses}) == 20
            and {client.get("role") for client in clients} == stations
            and variant is not None
            and state.get("pool_clients") == 100 and state.get("expected_online_clients") == 20
            and set(roles) == stations | mesh | dormant and present == stations | mesh)


def default_steering_budget(optimizer):
    return ("maximum_actions" in optimizer and optimizer["maximum_actions"] is None
            and optimizer.get("steering_safety", {}).get("enabled") is True)


def main():
    parser = argparse.ArgumentParser(description="Read-only default room freshness/convergence check.")
    parser.add_argument("--room-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--require-absolute-best", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    stable_since = None
    criterion = "absolute_best_converged" if args.require_absolute_best else "policy_converged"
    report = {"passed": False, "url": args.room_url, "started": time.time(), "convergence_criterion": criterion}
    with (args.output / "samples.jsonl").open("w") as stream:
        while time.monotonic() - started < args.timeout:
            sample = {"time": time.time()}
            try:
                states = {}
                for endpoint in ("current", "interactions"):
                    with urllib.request.urlopen(args.room_url.rstrip("/") + "/api/demo/" + endpoint, timeout=10) as response:
                        states[endpoint] = json.load(response)
                    (args.output / (endpoint + ".json")).write_text(json.dumps(states[endpoint], indent=2) + "\n")
                current, state = states["current"], states["interactions"]
                health = current.get("health") or {}
                optimizer = current.get("optimizer") or {}
                fleet = optimizer.get("fleet") or {}
                clients = current.get("network", {}).get("clients", [])
                now = dt.datetime.now(dt.timezone.utc)
                freshness = freshness_state(optimizer, clients, now)
                sample["timestamp_errors"] = freshness["timestamp_errors"]
                convergence = convergence_state(optimizer, clients)
                sample["convergence"] = convergence
                checks = {
                    "healthy": bool(health.get("healthy")),
                    "default_roster": default_roster(health, clients, state),
                    "paused_unleased": state["playback"]["status"] == "paused" and state["playback"]["time_ms"] == 0 and not state["lease"]["held"] and not state["fault"],
                    "steering_budget": default_steering_budget(optimizer),
                    "current_epoch": current["environment_epoch"] == state["environment_epoch"] == optimizer.get("environment_epoch"),
                    "complete": bool(fleet.get("measurement_complete")) and fleet.get("clients_checked") == 20 and fleet.get("clients_evaluated") == 20,
                    "converged": convergence[criterion],
                    "fresh": freshness["fresh"],
                }
                sample["checks"] = checks
                if all(checks.values()):
                    stable_since = stable_since or time.monotonic()
                    if time.monotonic() - stable_since >= 5:
                        report["passed"] = True
                else:
                    stable_since = None
            except (OSError, ValueError, KeyError, TypeError) as error:
                stable_since = None
                sample["error"] = str(error)
            stream.write(json.dumps(sample) + "\n")
            stream.flush()
            if report["passed"]:
                break
            time.sleep(1)
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    report["finished"] = time.time()
    (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
