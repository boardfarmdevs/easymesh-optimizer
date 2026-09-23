from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .experiments import ExperimentError, verify_matrix


def _normalize_numbers(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_normalize_numbers(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_numbers(item) for key, item in value.items()}
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(
        _normalize_numbers(value), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _members(group: str, roles: list[str]) -> list[str]:
    if group in {"all-clients", "present-clients"}:
        return roles
    if group == "mobile-clients":
        return [role for role in roles if role.startswith("sta_mobile_")]
    if group in {"odd-clients", "even-clients"}:
        parity = 1 if group == "odd-clients" else 0
        return [
            role for role in roles
            if (match := re.search(r"(\d+)$", role)) and int(match.group(1)) % 2 == parity
        ]
    if group in roles:
        return [group]
    raise ExperimentError(f"unknown traffic group {group!r}")


def _invocation(driver: str, container: str, schedule: dict[str, Any], duration_ms: int) -> list[str]:
    target = schedule.get("target", "10.0.0.1")
    seconds = max(1, (duration_ms + 999) // 1000)
    if driver == "ping":
        interval = float(schedule.get("interval_ms", 1000)) / 1000
        return [
            "lxc", "exec", container, "--", "ping", "-n", "-q",
            "-i", f"{interval:g}", "-w", str(seconds), str(target),
        ]
    if driver == "iperf3":
        command = [
            "lxc", "exec", container, "--", "iperf3", "-c", str(target),
            "--json", "--time", str(seconds),
        ]
        if "rate_mbps" in schedule:
            command.extend(("--bitrate", f"{int(schedule['rate_mbps'])}M"))
        if schedule.get("direction") == "downstream":
            command.append("--reverse")
        return command
    raise ExperimentError(f"driver {driver!r} has no command adapter")


def compile_traffic_plan(
    matrix: dict[str, Any], case_id: str, bindings: dict[str, Any]
) -> dict[str, Any]:
    verify_matrix(matrix)
    if bindings.get("schema") != "optimizer.lab-bindings.v1":
        raise ExperimentError("bindings schema must be optimizer.lab-bindings.v1")
    matches = [item for item in matrix["cases"] if item["id"] == case_id]
    if len(matches) != 1:
        raise ExperimentError(f"matrix contains no unique case {case_id!r}")
    case = matches[0]
    roles = case["world"]["station_roles"]
    role_bindings = bindings.get("roles", {})
    missing = sorted(set(roles) - set(role_bindings))
    if missing:
        raise ExperimentError(f"lab bindings do not cover station roles: {missing}")
    containers = [role_bindings[role] for role in roles]
    if len(containers) != len(set(containers)):
        raise ExperimentError("station roles must bind to unique containers")

    world_duration = int(case["world"]["duration_ms"])
    driver = case["traffic"]["driver"]
    events = []
    if driver != "none":
        for schedule_index, schedule in enumerate(case["traffic"].get("schedule", [])):
            start = int(schedule.get("start_ms", 0))
            if schedule.get("duration") == "world":
                duration = world_duration - start
            else:
                duration = int(schedule.get("duration_ms", schedule.get("on_ms", 0)))
            if start < 0 or duration <= 0 or start + duration > world_duration:
                raise ExperimentError(f"traffic schedule {schedule_index} lies outside the world")
            starts = [start]
            if "period_ms" in schedule:
                period = int(schedule["period_ms"])
                if period <= 0:
                    raise ExperimentError("traffic period_ms must be positive")
                starts = list(range(start, world_duration, period))
            members = _members(schedule["group"], roles)
            if not members:
                raise ExperimentError(
                    f"traffic group {schedule['group']!r} selects no station in this world"
                )
            for event_start in starts:
                event_duration = min(duration, world_duration - event_start)
                for role in members:
                    container = role_bindings[role]
                    events.append(
                        {
                            "time_ms": event_start,
                            "duration_ms": event_duration,
                            "role": role,
                            "container": container,
                            "driver": driver,
                            "command": _invocation(driver, container, schedule, event_duration),
                        }
                    )
    events.sort(key=lambda item: (item["time_ms"], item["role"]))
    result = {
        "schema": "optimizer.traffic-plan.v1",
        "case_id": case_id,
        "matrix_sha256": matrix["matrix_sha256"],
        "world_golden_sha256": case["world"]["golden_sha256"],
        "traffic_profile": case["traffic"]["id"],
        "status": case["status"],
        "missing_capabilities": case["missing_capabilities"],
        "duration_ms": world_duration,
        "events": events,
    }
    result["plan_sha256"] = _hash(result)
    return result


def load_json(path: str | Path) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ExperimentError(f"{path}: {error}") from error
