#!/usr/bin/env python3
import argparse
import datetime
import glob
import json
import os
from pathlib import Path
import signal
import select
import sys
import time


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


running = True


def stop(_signal, _frame):
    global running
    running = False


def cpu_busy(current, previous):
    elapsed = sum(current) - sum(previous) if previous else 0
    if elapsed <= 0:
        return None
    return round(100 * (elapsed - current[3] + previous[3] - current[4] + previous[4]) / elapsed, 2)


def process_stat(pid, value):
    if value is None:
        return None
    opening, closing = value.find("("), value.rfind(")")
    fields = value[closing + 1:].split()
    if opening < 0 or closing <= opening or len(fields) < 22:
        return None
    try:
        return {"pid": int(pid), "name": value[opening + 1:closing],
                "started_ticks": int(fields[19]), "cpu_ticks": int(fields[11]) + int(fields[12]),
                "resident_bytes": int(fields[21]) * os.sysconf("SC_PAGE_SIZE")}
    except ValueError:
        return None


def process_usage(current, previous, elapsed, ticks_per_second):
    rows = []
    for key, sample in current.items():
        prior = previous.get(key)
        if prior is None or elapsed <= 0 or sample["cpu_ticks"] < prior["cpu_ticks"]:
            continue
        usage = (sample["cpu_ticks"] - prior["cpu_ticks"]) * 100 / ticks_per_second / elapsed
        rows.append({**sample, "cpu_percent_one_core": round(usage, 2)})
    return sorted(rows, key=lambda row: (-row["cpu_percent_one_core"], row["pid"]))[:10]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bounded physical-host resource sampling")
    parser.add_argument("--interval", type=float, default=10)
    parser.add_argument("--duration", type=float, default=7200)
    parser.add_argument("--watch-stdin", action="store_true")
    parser.add_argument("--processes", action="store_true")
    parser.add_argument("--frequencies", action="store_true")
    args = parser.parse_args(argv)
    if not .1 <= args.interval <= 10 or not args.interval <= args.duration <= 7200:
        parser.error("requires 0.1..10 second interval and interval..7200 second duration")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    previous = {}
    previous_processes = {}
    previous_process_time = None
    deadline = time.monotonic() + args.duration
    while running and time.monotonic() < deadline:
        started = time.monotonic()
        counters = {line.split()[0]: list(map(int, line.split()[1:9]))
                    for line in read("/proc/stat").splitlines() if line.startswith("cpu")}
        temperatures = {}
        for directory in glob.glob("/sys/class/hwmon/hwmon*"):
            if read(directory + "/name") not in {"coretemp", "k10temp", "acpitz"}:
                continue
            for path in glob.glob(directory + "/temp*_input"):
                value = read(path)
                if value is not None:
                    temperatures[path] = int(value) / 1000
        memory = dict(line.split(":", 1) for line in read("/proc/meminfo").splitlines())
        frequencies = {path: int(value) for path in glob.glob(
            "/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq") if args.frequencies and (value := read(path)) is not None}
        processes = {}
        if args.processes:
            for path in glob.glob("/proc/[0-9]*/stat"):
                sample = process_stat(Path(path).parent.name, read(path))
                if sample is not None:
                    processes[(sample["pid"], sample["started_ticks"])] = sample
        process_time = time.monotonic()
        process_rows = process_usage(processes, previous_processes,
            process_time - previous_process_time if previous_process_time is not None else 0,
            os.sysconf("SC_CLK_TCK"))
        print(json.dumps({"time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "monotonic_ns": time.monotonic_ns(),
            "sampling_elapsed_ms": (time.monotonic() - started) * 1000,
            "cpu_busy_percent": cpu_busy(counters["cpu"], previous.get("cpu")),
            "per_cpu_busy_percent": {name: cpu_busy(values, previous.get(name))
                                     for name, values in counters.items() if name != "cpu"},
            "cpu_frequency_khz": frequencies,
            "intel_pstate_no_turbo": read("/sys/devices/system/cpu/intel_pstate/no_turbo"),
            "rapl_power_limits_uw": {path: read(path) for path in glob.glob(
                "/sys/class/powercap/intel-rapl:*/constraint_*_power_limit_uw")},
            "available_memory_kib": int(memory["MemAvailable"].split()[0]),
            "temperatures_celsius": temperatures,
            "package_throttle_count": read("/sys/devices/system/cpu/cpu0/thermal_throttle/package_throttle_count"),
            "package_throttle_total_time_ms": read("/sys/devices/system/cpu/cpu0/thermal_throttle/package_throttle_total_time_ms"),
            "package_throttle_max_time_ms": read("/sys/devices/system/cpu/cpu0/thermal_throttle/package_throttle_max_time_ms"),
            "top_processes": process_rows if args.processes else None,
            "cpu_pressure": read("/proc/pressure/cpu"), "memory_pressure": read("/proc/pressure/memory"),
            "io_pressure": read("/proc/pressure/io")}), flush=True)
        previous = counters
        previous_processes = processes
        previous_process_time = process_time
        next_sample = min(deadline, started + args.interval)
        while running and time.monotonic() < next_sample:
            remaining = min(1, next_sample - time.monotonic())
            if remaining <= 0:
                break
            if args.watch_stdin:
                if select.select([sys.stdin], [], [], remaining)[0] and not sys.stdin.read(1):
                    return 0
            else:
                time.sleep(remaining)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
