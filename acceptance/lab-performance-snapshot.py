#!/usr/bin/env python3
"""Capture a comparable EasyMesh lab lifecycle and memory snapshot.

Run this as root inside either outer lab VM after its lifecycle gate finishes.
The output schema is intentionally shared by the RDK and prplMesh labs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import socket
import subprocess
from pathlib import Path
from typing import Any


PROCESS_GROUPS = {
    "rdk": {
        "controller": ("onewifi_em_ctrl",),
        "agent": ("onewifi_em_agent",),
        "cli_webui": ("onewifi_em_cli",),
        "wifi_manager": ("/OneWifi", "OneWifi -subsys"),
        "ieee1905": ("/ieee1905",),
        "database": ("mariadbd", "mysqld"),
        "hostap": ("hostapd",),
        "supplicant": ("wpa_supplicant",),
    },
    "prplmesh": {
        "controller": ("beerocks_controller",),
        "agent": ("beerocks_agent",),
        "ieee1905": ("ieee1905_transport",),
        "hostap": ("hostapd",),
        "supplicant": ("wpa_supplicant",),
        "topology_adapter": ("topology-adapter", "server.py", "proxy.py"),
        "controller_ui": ("easymesh-controller",),
    },
    "shared": {
        "wmediumd_console": ("wmediumd-console",),
        "wmediumd": ("wmediumd",),
        "optimizer": ("optimizer",),
    },
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def read_key_values(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in path.read_text().splitlines():
            key, _, tail = line.partition(":")
            if not tail:
                continue
            match = re.search(r"\d+", tail)
            if match:
                values[key] = int(match.group())
    except (OSError, UnicodeDecodeError):
        pass
    return values


def read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def cmdline(proc: Path) -> str:
    try:
        value = proc.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode(
            errors="replace"
        ).strip()
        if value:
            return value
        return f"[{proc.joinpath('comm').read_text().strip()}]"
    except OSError:
        return ""


def cgroup_and_container(proc: Path) -> tuple[str, str | None]:
    try:
        lines = proc.joinpath("cgroup").read_text().splitlines()
    except OSError:
        return "", None
    value = "\n".join(lines)
    patterns = (
        r"(?:^|/)lxc\.payload\.([^/\n]+)",
        r"(?:^|/)lxc/([^/\n]+)",
        r"(?:^|/)lxc\.monitor\.([^/\n]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return value, match.group(1)
    return value, None


def classify(command: str, stack: str) -> str | None:
    for group, needles in PROCESS_GROUPS[stack].items():
        if any(needle in command for needle in needles):
            return group
    for group, needles in PROCESS_GROUPS["shared"].items():
        if any(needle in command for needle in needles):
            return group
    return None


def process_snapshot(stack: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    processes: list[dict[str, Any]] = []
    groups: dict[str, dict[str, int]] = {}
    ticks_per_second = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        command = cmdline(proc)
        group = classify(command, stack)
        if group is None:
            continue
        memory = read_key_values(proc / "smaps_rollup")
        status = read_key_values(proc / "status")
        try:
            stat = proc.joinpath("stat").read_text().split()
            cpu_seconds = (int(stat[13]) + int(stat[14])) / ticks_per_second
        except (OSError, IndexError, ValueError):
            cpu_seconds = 0.0
        try:
            fd_count = sum(1 for _ in proc.joinpath("fd").iterdir())
        except OSError:
            fd_count = -1
        cgroup, container = cgroup_and_container(proc)
        item = {
            "pid": int(proc.name),
            "group": group,
            "container": container,
            "command": command,
            "pss_kib": memory.get("Pss", 0),
            "rss_kib": memory.get("Rss", status.get("VmRSS", 0)),
            "pss_anon_kib": memory.get("Pss_Anon", 0),
            "pss_file_kib": memory.get("Pss_File", 0),
            "pss_shmem_kib": memory.get("Pss_Shmem", 0),
            "private_kib": memory.get("Private_Clean", 0)
            + memory.get("Private_Dirty", 0),
            "swap_kib": memory.get("Swap", 0),
            "threads": status.get("Threads", 0),
            "fd_count": fd_count,
            "cpu_seconds": round(cpu_seconds, 3),
            "cgroup": cgroup,
        }
        processes.append(item)
        aggregate = groups.setdefault(
            group,
            {
                "processes": 0,
                "pss_kib": 0,
                "rss_kib": 0,
                "private_kib": 0,
                "swap_kib": 0,
                "threads": 0,
                "fd_count": 0,
            },
        )
        aggregate["processes"] += 1
        for key in ("pss_kib", "rss_kib", "private_kib", "swap_kib", "threads"):
            aggregate[key] += int(item[key])
        if fd_count >= 0:
            aggregate["fd_count"] += fd_count
    processes.sort(key=lambda item: (-item["pss_kib"], item["pid"]))
    return processes, dict(sorted(groups.items()))


def nested_lxd() -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["lxc", "list", "--format", "json"],
            check=True,
            text=True,
            capture_output=True,
            timeout=30,
        )
        instances = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {"available": False, "instances": 0, "running": 0, "states": {}}
    states: dict[str, int] = {}
    names: list[str] = []
    for item in instances:
        status = str(item.get("status", "UNKNOWN")).lower()
        states[status] = states.get(status, 0) + 1
        names.append(str(item.get("name", "")))
    return {
        "available": True,
        "instances": len(instances),
        "running": states.get("running", 0),
        "states": dict(sorted(states.items())),
        "names": sorted(names),
    }


def lifecycle(stack: str, explicit: Path | None) -> dict[str, Any] | None:
    candidates = [explicit] if explicit else []
    if stack == "rdk":
        candidates += [
            Path("/home/easymesh/.local/state/easymesh-lab/reboot-acceptance/last-start-timing.json"),
            Path("/var/lib/easymesh-lab/last-start-timing.json"),
        ]
    else:
        candidates += [Path("/var/lib/prplmesh-lab/last-start-timing.json")]
    for path in candidates:
        if path is None or not path.is_file():
            continue
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        elapsed = 0
        milestones = []
        for phase in value.get("phases", []):
            elapsed += int(phase.get("elapsed_ms", 0))
            milestones.append(
                {"state": phase.get("phase"), "elapsed_ms": elapsed}
            )
        return {"source": str(path), "record": value, "milestones": milestones}
    return None


def elapsed_ms(started: str | None, finished: str | None) -> int | None:
    if not started or not finished:
        return None
    try:
        start = dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
        finish = dt.datetime.fromisoformat(finished.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, round((finish - start).total_seconds() * 1000))


def first_boot(stack: str) -> dict[str, Any] | None:
    if stack == "rdk":
        path = Path("/var/lib/easymesh-lab/thin-firstboot-report.json")
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        started = record.get("started_at")
        finished = record.get("finished_at")
        result = record.get("result")
    else:
        path = Path("/var/lib/prplmesh-lab/thin-firstboot-report.txt")
        try:
            fields = dict(
                line.split("=", 1)
                for line in path.read_text().splitlines()
                if "=" in line
            )
        except OSError:
            return None
        record = {
            key: int(value) if value.isdigit() else value
            for key, value in fields.items()
        }
        started = fields.get("thin_firstboot_started_at")
        finished = fields.get("thin_firstboot_finished_at")
        result = fields.get("thin_firstboot_status", "pending")
    return {
        "source": str(path),
        "started_at_utc": started,
        "finished_at_utc": finished,
        "elapsed_ms": elapsed_ms(started, finished),
        "result": result,
        "record": record,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", required=True, choices=("rdk", "prplmesh"))
    parser.add_argument("--profile", required=True, choices=("20", "50", "100"))
    parser.add_argument("--label", default="ready")
    parser.add_argument("--timing-file", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()

    meminfo = read_key_values(Path("/proc/meminfo"))
    processes, groups = process_snapshot(args.stack)
    relevant_totals = {
        key: sum(int(group.get(key, 0)) for group in groups.values())
        for key in ("processes", "pss_kib", "rss_kib", "private_kib", "swap_kib", "threads", "fd_count")
    }
    value = {
        "schema_version": 1,
        "collected_at_utc": utc_now(),
        "stack": args.stack,
        "profile_clients": int(args.profile),
        "label": args.label,
        "host": {
            "hostname": socket.gethostname(),
            "kernel": platform.release(),
            "uptime_seconds": float(Path("/proc/uptime").read_text().split()[0]),
            "load_average": list(os.getloadavg()),
            "mem_total_kib": meminfo.get("MemTotal"),
            "mem_available_kib": meminfo.get("MemAvailable"),
            "swap_total_kib": meminfo.get("SwapTotal"),
            "swap_free_kib": meminfo.get("SwapFree"),
            "cgroup_memory_current_bytes": read_int(Path("/sys/fs/cgroup/memory.current")),
            "cgroup_memory_peak_bytes": read_int(Path("/sys/fs/cgroup/memory.peak")),
        },
        "nested_lxd": nested_lxd(),
        "first_boot": first_boot(args.stack),
        "lifecycle": lifecycle(args.stack, args.timing_file),
        "relevant_process_totals": relevant_totals,
        "process_groups": groups,
        "processes": processes,
    }
    encoded = json.dumps(value, indent=None if args.compact else 2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
        temporary.write_text(encoded)
        temporary.replace(args.output)
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
