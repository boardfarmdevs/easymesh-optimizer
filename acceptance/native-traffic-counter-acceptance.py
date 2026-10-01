#!/usr/bin/env python3
"""Qualify native EasyMesh STA traffic counters against one bounded UDP endpoint run."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
import os
from pathlib import Path
import statistics
import subprocess
import time

from optimizer.load_observer import NativeLoadProvider
from room_service.traffic_experiment import namespace_udp


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", choices=("rdk", "prpl"), required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        print(json.dumps({"state": "prepared", "scope": "bounded native traffic-counter qualification"}))
        return 0
    require(os.geteuid() == 0, "requires root inside the lab VM")
    args.output.mkdir(parents=True, exist_ok=False)
    rdk = args.stack == "rdk"
    controller = "bpibroadband" if rdk else "prpl-controller"
    target = "10.0.0.1" if rdk else "192.168.77.1"
    binding = json.loads((args.root / "rooms/bindings/private-client-room-walk.json").read_text())["roles"]
    container = binding["sta_static_01"]
    station = subprocess.run(["lxc", "exec", container, "--", "cat", "/sys/class/net/wlan0/address"],
                             check=True, capture_output=True, text=True, timeout=5).stdout.strip().lower()
    if rdk:
        from optimizer.observer import ControllerObserver
        observer = ControllerObserver()
    else:
        from optimizer.prplmesh import PrplMeshObserver
        observer = PrplMeshObserver()
    counter_unit_bytes = 1 if rdk else 1024
    provider = NativeLoadProvider(controller, byte_counter_unit_bytes=counter_unit_bytes)
    observer.ownership_observer = provider.observe_owners
    samples = []
    report = {"stack": args.stack, "state": "failed", "station": station,
              "source_container": container, "endpoint": None, "native_samples": []}
    try:
        def collect(label):
            snapshot = provider.enrich(observer.observe(), observer.last_raw)
            rows = [row.to_dict() if hasattr(row, "to_dict") else row.__dict__
                    for row in snapshot.client_activity if row.sta_mac == station]
            samples.append({"phase": label, "observed_at": snapshot.observed_at,
                            "client_activity": rows})
            report["native_samples"].extend(rows)

        baseline_deadline = time.monotonic() + (12 if rdk else 4)
        while time.monotonic() < baseline_deadline:
            collect("baseline")
            time.sleep(.25)
        phase = {"role": "sta_static_01", "start_ms": 0, "end_ms": 15000,
                 "mode": "udp", "offered_mbps": 8, "payload_bytes": 1200}
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(namespace_udp, container, target, phase, 15000,
                                     admit=nullcontext, register=lambda _process: None,
                                     running=lambda _value: None)
            while not future.done():
                collect("traffic")
                time.sleep(.25)
            report["endpoint"] = future.result()
        for _iteration in range(3):
            collect("recovery")
            time.sleep(.25)
        endpoint = report["endpoint"]
        require(endpoint.get("state") == "completed", "bounded UDP endpoint did not complete")
        qualified = [row for row in report["native_samples"]
                     if row.get("bytes_per_second") is not None
                     and row.get("retries_per_second") is not None
                     and row.get("errors_per_second") is not None]
        require(qualified, "native byte/retry/error counter deltas remained unavailable")
        positive = [row for row in qualified if row["bytes_per_second"] > 0]
        require(positive, "native byte counters did not increase during bounded traffic")
        peak = max(qualified, key=lambda row: row["bytes_per_second"])
        median_bytes = statistics.median(row["bytes_per_second"] for row in positive)
        representative = min(positive, key=lambda row: abs(row["bytes_per_second"] - median_bytes))
        native_bits = median_bytes * 8
        sender_bits = endpoint["sender"]["bits_per_second"]
        ratio = native_bits / sender_bits
        require(.25 <= ratio <= 2.0, "native byte unit is inconsistent with bounded endpoint traffic")
        require(all(row["packets_per_second"] >= 0 and row["retries_per_second"] >= 0
                    and row["errors_per_second"] >= 0 for row in qualified),
                "native counter rates are negative")
        report.update(state="passed", native_representative=representative, native_peak=peak,
                      native_to_sender_bit_rate_ratio=ratio,
                      source_byte_counter_unit_bytes=counter_unit_bytes,
                      interpretation="source counters are normalized to octets; retry/error zero is valid; rates are not airtime or demand")
    except Exception as error:
        report["error"] = str(error)
    finally:
        provider.close()
        (args.output / "samples.jsonl").write_text("".join(json.dumps(row) + "\n" for row in samples))
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0 if report["state"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
