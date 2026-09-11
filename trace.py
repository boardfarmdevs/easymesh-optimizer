from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import threading
import time

from .journal import BoundedJournal


class TraceSink:
    def __init__(self, directory: Path):
        self.sequence = 0
        self.previous_hash = None
        self.journal = BoundedJournal(directory / "live-events.jsonl", segment_bytes=16 * 1024 * 1024,
                                      segments=8, queue_bytes=4 * 1024 * 1024)

    def emit(self, kind: str, payload: dict):
        self.sequence += 1
        event = {"sequence": self.sequence, "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                 "receipt_monotonic_ns": time.monotonic_ns(), "kind": kind, "payload": payload,
                 "previous_event_hash": self.previous_hash}
        event["event_hash"] = hashlib.sha256(json.dumps(event, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.previous_hash = event["event_hash"]
        self.journal.append(event, (json.dumps(event, separators=(",", ":")) + "\n").encode())


def take_lines(buffer: bytes, chunk: bytes):
    combined = buffer + chunk
    if len(combined) > 65536:
        raise ValueError("client control trace exceeded its bounded line buffer")
    parts = combined.replace(b"\r", b"\n").split(b"\n")
    return parts[-1], [part.decode("utf-8", errors="replace").strip(" >") for part in parts[:-1] if part.strip(b" >")]


def capture(directory: Path, duration: float, raw_monitor: bool):
    directory.mkdir(parents=True, exist_ok=False)
    inventory = json.loads(subprocess.check_output(["lxc", "list", "--format=json"]))
    clients = [node for node in inventory if node["name"].startswith("wlan-client") and node["status"] == "Running"]
    controllers = [node for node in inventory if node["name"] == "bpibroadband" and node["status"] == "Running"]
    if not clients or len(controllers) != 1:
        raise RuntimeError("trace requires running WLAN clients and one bpibroadband controller")
    (directory / "inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
    sink = TraceSink(directory)
    selector = selectors.DefaultSelector()
    stopped = threading.Event()
    previous_signals = {number: signal.signal(number, lambda *_: stopped.set()) for number in (signal.SIGINT, signal.SIGTERM)}
    processes = []
    logs = []
    raised_monitor = False
    errors = []
    capture_names = []
    started = time.monotonic()
    summary = {"schema": "easymesh.coordination-trace.v1", "client_count": len(clients),
               "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
               "raw_monitor": raw_monitor, "raw_monitor_semantics": "transmitter-side hwsim monitor, not receiver delivery proof",
               "client_clock": "collector receipt time, not supplicant source timestamp",
               "controller_clock": "kernel packet capture wall clock", "errors": errors}
    try:
        sink.emit("trace.started", summary)
        captures = [("cmdu", ["nsenter", "--target", str(controllers[0]["state"]["pid"]), "--net"],
                     "brlan0", "ether proto 0x893a")]
        if raw_monitor:
            link = json.loads(subprocess.check_output(["ip", "-j", "link", "show", "hwsim0"]))[0]
            if "UP" not in link["flags"]:
                subprocess.run(["ip", "link", "set", "hwsim0", "up"], check=True)
                raised_monitor = True
            captures.append(("wifi", [], "hwsim0", "type mgt and not (subtype beacon or subtype probe-req or subtype probe-resp)"))
        for name, prefix, interface, packet_filter in captures:
            capture_names.append(name)
            log = (directory / (name + "-capture.log")).open("wb")
            logs.append(log)
            process = subprocess.Popen(prefix + ["tcpdump", "-i", interface, "-nn", "-s", "0", "-B", "4096",
                "-U", "-C", "16", "-W", "8", "-Z", "root", "-w", str(directory / (name + ".pcap")), packet_filter],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log)
            processes.append((name, process, False))
        for node in clients:
            process = subprocess.Popen(["lxc", "exec", node["name"], "--mode=non-interactive", "--", "wpa_cli", "-i", "wlan0"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
            processes.append((node["name"], process, True))
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, {"name": node["name"], "buffer": b"", "lines": 0, "last_status": None})
            process.stdin.write(b"status\n")
        last_status = time.monotonic()
        while not stopped.is_set() and time.monotonic() - started < duration:
            for key, _ in selector.select(timeout=0.2):
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    raise RuntimeError("client event stream closed: " + key.data["name"])
                key.data["buffer"], lines = take_lines(key.data["buffer"], chunk)
                for line in lines:
                    key.data["lines"] += 1
                    if line.startswith("wpa_state="):
                        key.data["last_status"] = time.monotonic()
                    sink.emit("trace.client", {"container": key.data["name"], "line": line})
            for name, process, _ in processes:
                if process.poll() is not None:
                    raise RuntimeError(f"trace process exited: {name} ({process.returncode})")
            if time.monotonic() - last_status >= 5:
                for _, process, client in processes:
                    if client:
                        process.stdin.write(b"status\n")
                last_status = time.monotonic()
            if not sink.journal.status()["complete"]:
                raise RuntimeError(sink.journal.status()["error"])
            if time.monotonic() - started > 15:
                for key in selector.get_map().values():
                    if key.data["last_status"] is None or time.monotonic() - key.data["last_status"] > 15:
                        raise RuntimeError("client status heartbeat missing: " + key.data["name"])
    except Exception as error:
        errors.append(str(error))
    finally:
        summary["client_streams"] = {key.data["name"]: key.data["lines"] for key in selector.get_map().values()}
        for name, process, client in processes:
            if process.poll() is None:
                try:
                    if client:
                        process.stdin.write(b"quit\n")
                        process.stdin.close()
                    else:
                        process.send_signal(signal.SIGINT)
                except (BrokenPipeError, OSError) as error:
                    errors.append(f"{name}: {error}")
        deadline = time.monotonic() + 5
        for name, process, _ in processes:
            try:
                process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                errors.append(f"trace process needed termination: {name}")
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for log in logs:
            log.close()
        summary["captures"] = {}
        for name in capture_names:
            log_text = (directory / (name + "-capture.log")).read_text(errors="replace")
            dropped = re.search(r"(\d+) packets dropped by kernel", log_text)
            packets = re.search(r"(\d+) packets captured", log_text)
            files = sorted(directory.glob(name + ".pcap*"))
            complete = dropped is not None and int(dropped.group(1)) == 0 and len(files) < 8
            summary["captures"][name] = {"kernel_dropped": int(dropped.group(1)) if dropped else None,
                "packets": int(packets.group(1)) if packets else None, "files": [path.name for path in files],
                "history_complete": complete, "ring_bytes_bound": 128000000}
            if not complete:
                errors.append(name + " capture dropped packets, lacks final counters or may have wrapped its ring")
        selector.close()
        if raised_monitor:
            restored = subprocess.run(["ip", "link", "set", "hwsim0", "down"], capture_output=True)
            if restored.returncode:
                errors.append("could not restore hwsim0 to down: " + restored.stderr.decode(errors="replace"))
        summary["duration_seconds"] = time.monotonic() - started
        summary["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        sink.emit("trace.finished", summary)
        sink.journal.close()
        summary["journal"] = sink.journal.status()
        summary["complete"] = not errors and summary["journal"]["complete"] and not summary["journal"]["history_truncated"]
        (directory / "trace-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        for number, handler in previous_signals.items():
            signal.signal(number, handler)
    return summary


def main():
    parser = argparse.ArgumentParser(description="Opt-in bounded RDK client/CMDU evidence; does not issue RF or steering commands.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=600)
    parser.add_argument("--raw-monitor", action="store_true", help="Enable hwsim0 temporarily; only use on a separately qualified kernel/medium combination.")
    arguments = parser.parse_args()
    if arguments.duration <= 0:
        parser.error("duration must be positive")
    summary = capture(arguments.output.resolve(), arguments.duration, arguments.raw_monitor)
    print(json.dumps(summary, indent=2))
    return 0 if summary["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
