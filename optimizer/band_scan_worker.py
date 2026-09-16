from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import uuid


def coordinated_scan(connection, station, bssid, ssid, frequencies, *, clock=None, boottime=None, dump=None):
    clock = clock or time.monotonic
    boottime = boottime or (lambda: time.clock_gettime(time.CLOCK_BOOTTIME))
    deadline = clock() + 5
    events = []

    def receive():
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("supplicant scan completion deadline exceeded")
        connection.settimeout(remaining)
        return connection.recv(65536).decode("utf-8", errors="strict").strip()

    def request(command):
        connection.send(command.encode())
        for _attempt in range(4096):
            reply = receive()
            if reply.startswith("<"):
                events.append(reply)
            else:
                return reply
        raise RuntimeError("supplicant event flood obscured command response")

    def check_status():
        status = dict(line.split("=", 1) for line in request("STATUS").splitlines() if "=" in line)
        if (status.get("address", "").lower() != station or status.get("bssid", "").lower() != bssid
                or status.get("ssid") != ssid or status.get("wpa_state") != "COMPLETED"):
            raise RuntimeError("supplicant scan association identity changed")
        if int(status.get("freq", 0)) not in frequencies:
            raise RuntimeError("scan must include the current serving frequency")
        return status

    if request("ATTACH") != "OK":
        raise RuntimeError("could not attach to native supplicant events")
    before = check_status()
    started = boottime()
    reply = request("SCAN TYPE=ONLY freq=" + ",".join(map(str, frequencies)) + " passive=1 only_new=1 use_id=1")
    if not reply.isdecimal():
        raise RuntimeError("supplicant scan request rejected: " + reply)
    scan_id = int(reply)
    for _attempt in range(4096):
        event = events.pop(0) if events else receive()
        if "CTRL-EVENT-DISCONNECTED" in event:
            raise RuntimeError("client disconnected during supplicant scan")
        if "CTRL-EVENT-SCAN-FAILED" in event:
            raise RuntimeError("native supplicant scan failed: " + event)
        observed = re.search(r"CTRL-EVENT-SCAN-RESULTS\b.*\bid=(\d+)\b", event)
        if observed and int(observed[1]) == scan_id:
            completed = boottime()
            raw = dump() if dump is not None else None
            after = check_status()
            if any(before.get(key) != after.get(key) for key in ("address", "bssid", "freq", "ssid", "wpa_state")):
                raise RuntimeError("client changed association during scan")
            return {"scan_id": scan_id, "started_boottime": started, "completed_boottime": completed,
                    "completion_event": event, "transport": "wpa_control_scan_only_nl80211_dump", "mode": "passive",
                    "before": before, "after": after, "raw_scan": raw}
    raise RuntimeError("native scan completion was not observed")


def main(arguments):
    if Path("/sys/module/mac80211_hwsim/parameters/rx_context_reports").read_text().strip() != "Y":
        raise RuntimeError("passive band scans require native hwsim receive-context reporting")
    process, station, bssid, ssid, *frequencies = arguments
    if not process.isdecimal() or int(process) <= 1 or ssid not in {"private_ssid", "iot_ssid"}:
        raise ValueError("invalid native scan identity")
    if not all(re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", address) for address in (station, bssid)):
        raise ValueError("invalid native scan MAC address")
    frequencies = [int(value) for value in frequencies]
    if not 1 <= len(frequencies) <= 16 or any(not 2412 <= value <= 7115 for value in frequencies):
        raise ValueError("invalid native scan frequencies")
    def dump():
        return subprocess.run(
            ["nsenter", "--target", process, "--mount", "--root", "--wd", "--",
             "iw", "dev", "wlan0", "scan", "dump"],
            capture_output=True, text=True, check=True, timeout=2).stdout

    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
        connection.bind("\0lab-band-scan-" + str(os.getpid()) + "-" + uuid.uuid4().hex)
        connection.connect(f"/proc/{process}/root/run/wpa_supplicant/wlan0")
        try:
            print(json.dumps(coordinated_scan(connection, station, bssid, ssid, frequencies, dump=dump)))
        finally:
            try:
                connection.send(b"DETACH")
            except OSError:
                pass


if __name__ == "__main__":
    main(sys.argv[1:])
