from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

from .candidates import CandidateMetricsUnavailable
from .model import format_time, normalize_mac


SOURCE = "client_nl80211_received_scan"
CONTAINER = re.compile(r"(?:prpl-client-[0-9]{2,3}|wlan-client(?:-[0-9]{3})?)")
_RADIO_LOCKS = {}
_RADIO_LOCK = threading.Lock()


def client_radio_lock(container):
    if not isinstance(container, str) or not CONTAINER.fullmatch(container):
        raise ValueError("radio coordination requires a bound WLAN client container")
    with _RADIO_LOCK:
        return _RADIO_LOCKS.setdefault(container, threading.Lock())


def frequency_band(frequency):
    if type(frequency) is not int:
        raise ValueError("frequency must be an integer")
    if 2412 <= frequency <= 2484:
        return "2.4"
    if 5000 <= frequency < 5925:
        return "5"
    if 5955 <= frequency <= 7115:
        return "6"
    raise ValueError("unsupported Wi-Fi frequency")


def parse_scan(text, *, frequencies, started_boottime, finished_boottime):
    if (len(text) > 512 * 1024 or not math.isfinite(started_boottime)
            or not math.isfinite(finished_boottime) or finished_boottime < started_boottime):
        raise ValueError("invalid scan interval or oversized result")
    observed = {}
    for block in re.split(r"(?m)^BSS ", text)[1:]:
        header = re.match(r"([0-9a-fA-F:]{17})\(on wlan0\)(?: -- associated)?\s*$", block.splitlines()[0])
        frequency = re.search(r"(?m)^\s+freq:\s*(\d+(?:\.0+)?)\s*$", block)
        signal = re.search(r"(?m)^\s+signal:\s*(-?\d+(?:\.\d+)?) dBm\s*$", block)
        seen = re.search(r"(?m)^\s+last seen:\s*(\d+(?:\.\d+)?)s \[boottime\]\s*$", block)
        ssid = re.search(r"(?m)^\s+SSID: ([^\r\n]*)$", block)
        if not all((header, frequency, signal, seen)):
            raise ValueError("scan BSS lacks receiver identity, timestamp, frequency or signal")
        if ssid is None or not ssid[1]:
            continue
        bssid = normalize_mac(header[1])
        frequency_mhz = int(float(frequency[1]))
        rssi_dbm = float(signal[1])
        observed_boottime = float(seen[1])
        if not -110 <= rssi_dbm <= 0:
            raise ValueError("scan signal is outside the RCPI range")
        if frequency_mhz not in frequencies or observed_boottime + 0.001 < started_boottime:
            continue
        if observed_boottime > finished_boottime + 0.001:
            raise ValueError("scan contains a future receiver timestamp")
        if bssid in observed:
            raise ValueError("scan contains a duplicate BSSID")
        security = re.search(r"(?m)^\s+\* Authentication suites: ([^\r\n]+)$", block)
        load_heading = re.search(r"(?m)^\s+BSS Load:\s*$", block)
        load_fields = {
            "station_count": re.search(r"(?m)^\s+\* station count:\s*(\d+)\s*$", block),
            "utilization": re.search(r"(?m)^\s+\* channel utilisation:\s*(\d+)/255\s*$", block),
            "admission_capacity": re.search(
                r"(?m)^\s+\* available admission capacity:\s*(\d+)\s+\[\*32us\]\s*$", block),
        }
        if load_heading and not all(load_fields.values()):
            raise ValueError("scan BSS Load element is incomplete")
        advertised_load = {"state": "unavailable"}
        if load_heading:
            advertised_load = {"state": "available", **{
                name: int(match[1]) for name, match in load_fields.items()}}
            if (advertised_load["station_count"] > 65535 or advertised_load["utilization"] > 255
                    or advertised_load["admission_capacity"] > 65535):
                raise ValueError("scan BSS Load element is outside its wire range")
        observed[bssid] = {
            "bssid": bssid, "frequency_mhz": frequency_mhz,
            "band": frequency_band(frequency_mhz), "ssid": ssid[1],
            "rssi_dbm": rssi_dbm, "rcpi": round(2 * (rssi_dbm + 110)),
            "observed_boottime": observed_boottime,
            "key_management": security[1].split() if security else [],
            "pmf_capable": "MFP-capable" in block,
            "pmf_required": "MFP-required" in block,
            "advertised_bss_load": advertised_load,
        }
    return observed


def parse_capabilities(info, phy, key_management):
    address = re.search(r"(?m)^\s*addr ([0-9a-fA-F:]{17})\s*$", info)
    physical = re.search(r"(?m)^\s*wiphy (\d+)\s*$", info)
    if not address or not physical:
        raise ValueError("client radio identity is unavailable")
    frequencies = set()
    for match in re.finditer(r"(?m)^\s*\* (\d+(?:\.0+)?) MHz \[[^\]]+\]([^\r\n]*)$", phy):
        if "disabled" in match[2]:
            continue
        frequency = int(float(match[1]))
        try:
            frequency_band(frequency)
        except ValueError:
            continue
        frequencies.add(frequency)
    methods = key_management.split()
    if not frequencies or not set(methods) & {"WPA-PSK", "SAE"}:
        raise ValueError("client has no supported personal-security scan profile")
    return {"sta_mac": normalize_mac(address[1]), "phy": int(physical[1]),
            "frequencies_mhz": sorted(frequencies),
            "bands": sorted({frequency_band(frequency) for frequency in frequencies}),
            "key_management": methods}


def _command(*arguments, timeout=6):
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CandidateMetricsUnavailable(f"native client scan command failed: {error}") from error
    if result.returncode:
        raise CandidateMetricsUnavailable(f"native client scan command failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


class NativeBandScanner:
    def __init__(self, command=None, *, boottime=None, clock=None):
        self.command = command or _command
        self.boottime = boottime or (lambda: time.clock_gettime(time.CLOCK_BOOTTIME))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._locks = {}
        self._lock = threading.Lock()

    @staticmethod
    def _validate_container(container):
        if not isinstance(container, str) or not CONTAINER.fullmatch(container):
            raise ValueError("scan requires a bound WLAN client container")

    def capabilities(self, container):
        self._validate_container(container)
        info = self.command("lxc", "exec", container, "--", "iw", "dev", "wlan0", "info")
        phy = re.search(r"(?m)^\s*wiphy (\d+)\s*$", info)
        if not phy:
            raise CandidateMetricsUnavailable("client radio identity is unavailable")
        details = self.command("lxc", "exec", container, "--", "iw", "phy", f"phy{phy[1]}", "info")
        methods = self.command("lxc", "exec", container, "--", "wpa_cli", "-i", "wlan0", "get_capability", "key_mgmt")
        return parse_capabilities(info, details, methods)

    def _scan(self, container, sta_mac, bssid, ssid, frequencies):
        state = json.loads(self.command("lxc", "query", f"/1.0/instances/{container}/state"))
        process = state.get("pid")
        if type(process) is not int or process <= 1:
            raise CandidateMetricsUnavailable("client network namespace is unavailable")
        worker = str(Path(__file__).with_name("band_scan_worker.py"))
        native = json.loads(self.command("nsenter", "--target", str(process), "--net", "--", sys.executable,
                                         worker, str(process), sta_mac, bssid, ssid, *map(str, frequencies)))
        return native.pop("raw_scan"), native

    def collect(self, container, *, sta_mac, bssid, ssid, frequencies, capabilities):
        self._validate_container(container)
        sta_mac, bssid = normalize_mac(sta_mac), normalize_mac(bssid)
        frequencies = sorted(set(frequencies))
        if (not 1 <= len(frequencies) <= 16 or capabilities.get("sta_mac") != sta_mac
                or not set(frequencies) <= set(capabilities.get("frequencies_mhz", []))):
            raise ValueError("scan frequencies or station capability identity are invalid")
        for frequency in frequencies:
            frequency_band(frequency)
        if ssid not in {"private_ssid", "iot_ssid"}:
            raise ValueError("scan requires a known lab SSID")
        with self._lock:
            lock = self._locks.setdefault(container, threading.Lock())
        if not lock.acquire(blocking=False):
            raise CandidateMetricsUnavailable("client scan is already in flight")
        radio_lock = client_radio_lock(container)
        if not radio_lock.acquire(blocking=False):
            lock.release()
            raise CandidateMetricsUnavailable("client radio is reserved for band settings")
        try:
            started = self.boottime()
            wall_started = self.clock()
            raw, native = self._scan(container, sta_mac, bssid, ssid, frequencies)
            finished = self.boottime()
            wall_finished = self.clock()
            before = native["before"]
            if (before.get("address", "").lower() != sta_mac or before.get("bssid", "").lower() != bssid
                    or before.get("ssid") != ssid or before.get("wpa_state") != "COMPLETED"):
                raise CandidateMetricsUnavailable("client scan association identity changed")
            if int(before.get("freq", 0)) not in frequencies:
                raise ValueError("scan must include the current serving frequency")
            if abs((wall_finished - wall_started).total_seconds() - (finished - started)) > 0.05:
                raise CandidateMetricsUnavailable("wall clock changed during client scan")
            after = native["after"]
            if any(before.get(key) != after.get(key) for key in ("address", "bssid", "freq", "ssid", "wpa_state")):
                raise CandidateMetricsUnavailable("client changed association during scan")
            if not started <= native["started_boottime"] <= native["completed_boottime"] <= finished:
                raise CandidateMetricsUnavailable("native supplicant scan clock interval is invalid")
            samples = parse_scan(raw, frequencies=frequencies, started_boottime=native["started_boottime"], finished_boottime=finished)
            samples = {address: sample for address, sample in samples.items() if sample["ssid"] == ssid}
            if bssid not in samples:
                raise CandidateMetricsUnavailable("scan has no fresh serving-BSS reference")
            for sample in samples.values():
                sample["observed_at"] = format_time(wall_started + timedelta(seconds=sample["observed_boottime"] - started))
            return {"source": SOURCE, "scan_id": uuid.uuid4().hex, "sta_mac": sta_mac,
                    "serving_bssid": bssid, "ssid": ssid, "requested_frequencies_mhz": frequencies,
                    "started_at": format_time(wall_started), "finished_at": format_time(wall_finished),
                    "elapsed_ms": round((finished - started) * 1000, 3), "samples": samples,
                    "native": native}
        except (ValueError, KeyError) as error:
            raise CandidateMetricsUnavailable(str(error)) from error
        finally:
            radio_lock.release()
            lock.release()
