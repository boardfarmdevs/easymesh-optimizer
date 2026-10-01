from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import re
import threading
import time

from optimizer.band_scan import NativeBandScanner, _command, client_radio_lock
from wmdcfg.actuator import ActuatorError


FIELDS = ("freq_list", "scan_freq", "key_mgmt", "ieee80211w", "sae_pwe")


def validate_profiles(world):
    profiles = world.get("band_steering", {})
    if not isinstance(profiles, dict) or len(profiles) > 4:
        raise ValueError("band steering supports at most four explicitly profiled clients")
    for role, profile in profiles.items():
        if world["roles"].get(role) != "station" or not world["generations"][0]["present"].get(role):
            raise ValueError("band profiles require an initially present bound station")
        if (not isinstance(profile, dict) or not {"allowed_bands", "initial_band"} <= set(profile)
                or set(profile) - {"allowed_bands", "initial_band", "measurement_mode"}):
            raise ValueError("band profiles require allowed_bands and initial_band")
        bands = profile["allowed_bands"]
        if (not isinstance(bands, list) or not bands or any(band not in ("2.4", "5", "6") for band in bands)
                or len(set(bands)) != len(bands) or profile["initial_band"] not in bands):
            raise ValueError("invalid or duplicate allowed bands or initial band")
        if "measurement_mode" in profile and (
                profile["measurement_mode"] != "received_same_band" or len(bands) != 1):
            raise ValueError("received_same_band requires exactly one allowed band")
    return copy.deepcopy(profiles)


class ClientBandSettings:
    def __init__(self, command=None, *, clock=None, sleep=None):
        self.command = command or _command
        self._native_commands = command is None
        self._sessions = threading.local()
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep

    @contextmanager
    def _session(self, container):
        NativeBandScanner._validate_container(container)
        current = getattr(self._sessions, "current", None)
        if current is not None:
            if current["container"] != container:
                raise ActuatorError("band settings namespace changed within a transaction")
            yield current
            return
        current = {"container": container, "process": None}
        self._sessions.current = current
        try:
            yield current
        finally:
            del self._sessions.current

    @staticmethod
    def _start_ticks(process):
        try:
            fields = Path(f"/proc/{process}/stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] in {"Z", "X", "x"}:
                raise ValueError("client init exited")
            return int(fields[19])
        except (OSError, IndexError, ValueError) as error:
            raise ActuatorError("band settings client namespace is no longer available") from error

    def control(self, container, *arguments):
        with self._session(container) as session:
            if not self._native_commands:
                return self.command("lxc", "exec", container, "--", "wpa_cli", "-i", "wlan0", *arguments).strip()
            if session["process"] is None:
                state = json.loads(self.command("lxc", "query", f"/1.0/instances/{container}/state", timeout=5))
                process = state.get("pid")
                if state.get("status") != "Running" or type(process) is not int or process <= 1:
                    raise ActuatorError("band settings client namespace is unavailable")
                session.update(process=process, start_ticks=self._start_ticks(process),
                               user_namespace=self._user_namespace_arguments(process))
            process = session["process"]
            if self._start_ticks(process) != session["start_ticks"]:
                raise ActuatorError("band settings client namespace identity changed")
            result = self.command("nsenter", "--target", str(process), *session["user_namespace"],
                                  "--mount", "--net", "--pid", "--root", "--wd",
                                  "--", "/usr/bin/env", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                                  "wpa_cli", "-i", "wlan0", *arguments).strip()
            if self._start_ticks(process) != session["start_ticks"]:
                raise ActuatorError("band settings client namespace identity changed")
            return result

    @staticmethod
    def _user_namespace_arguments(process):
        try:
            shared = Path(f"/proc/{process}/ns/user").samefile("/proc/self/ns/user")
        except OSError as error:
            raise ActuatorError("band settings client user namespace is unavailable") from error
        return () if shared else ("--user",)

    def _ok(self, container, *arguments):
        result = self.control(container, *arguments)
        if result != "OK":
            raise ActuatorError(f"{container}: band settings {' '.join(arguments[:3])} failed: {result}")

    def capture(self, container, sta_mac):
        with self._session(container):
            return self._capture(container, sta_mac)

    def _capture(self, container, sta_mac):
        status = dict(line.split("=", 1) for line in self.control(container, "status").splitlines() if "=" in line)
        if status.get("address", "").lower() != sta_mac:
            raise ActuatorError("client band profile identity changed")
        networks = []
        for line in self.control(container, "list_networks").splitlines()[1:]:
            fields = line.split("\t")
            if len(fields) in {3, 4} and fields[0].isdigit() and fields[1] in {"private_ssid", "iot_ssid"}:
                if len(fields) == 3:
                    fields.append("")
                networks.append(fields)
        if len(networks) != 1:
            raise ActuatorError("band settings require exactly one personal-security lab network")
        network_id, ssid, _bssid, _flags = networks[0]
        values = {field: self.control(container, "get", field) if field == "sae_pwe" else
                  self.control(container, "get_network", network_id, field) for field in FIELDS}
        for field, value in values.items():
            if value == "FAIL" and field not in {"freq_list", "scan_freq"}:
                raise ActuatorError(f"cannot capture client setting {field}")
        return {"container": container, "sta_mac": sta_mac, "network_id": network_id, "ssid": ssid,
                "values": {field: None if value == "FAIL" else value for field, value in values.items()}}

    def write(self, record, values):
        with self._session(record["container"]):
            self._write(record, values)

    def _write(self, record, values):
        observed = self.capture(record["container"], record["sta_mac"])
        if any(observed[key] != record[key] for key in ("container", "sta_mac", "network_id", "ssid")):
            raise ActuatorError("client network identity changed before band settings write")
        if set(values) != set(FIELDS):
            raise ActuatorError("incomplete client band settings")
        for field, value in values.items():
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9 -]{1,256}", value)):
                raise ActuatorError("invalid client band setting value")
            if observed["values"][field] == value:
                continue
            if field == "sae_pwe":
                self._ok(record["container"], "set", field, value)
            else:
                self._ok(record["container"], "set_network", record["network_id"], field, value or "")
        readback = self.capture(record["container"], record["sta_mac"])
        if readback["values"] != values:
            raise ActuatorError("client band settings readback mismatch")

    def restore(self, record, *, reconnect=True, wait=False):
        with client_radio_lock(record["container"]), self._session(record["container"]):
            if "sae_pwe" not in record["values"]:
                record = {**record, "values": {**record["values"], "sae_pwe": self.control(record["container"], "get", "sae_pwe")}}
            if record["values"]["ieee80211w"] == "3":
                current = self.capture(record["container"], record["sta_mac"])
                if current["values"]["ieee80211w"] != "3":
                    self._ok(record["container"], "reconfigure")
            self.write(record, record["values"])
            if reconnect:
                self._ok(record["container"], "reassociate")
                if wait:
                    frequencies = record["values"]["freq_list"] or record["values"]["scan_freq"]
                    self._wait_association(record, set(map(int, frequencies.split())) if frequencies else None, "restored")
            else:
                self._ok(record["container"], "disconnect")

    def initialize(self, record, values, initial_frequencies):
        with client_radio_lock(record["container"]), self._session(record["container"]):
            self._initialize(record, values, initial_frequencies)

    def _initialize(self, record, values, initial_frequencies):
        initial = " ".join(map(str, sorted(initial_frequencies)))
        self.write(record, {**values, "freq_list": initial, "scan_freq": initial})
        # The world's RF has just changed. Scan results the client took under the
        # previous room's RF can still count as fresh, and the client would join
        # that room's best AP; select from a scan of this room's RF instead.
        self._ok(record["container"], "bss_flush", "0")
        self._scan_passive(record, initial_frequencies)
        self._ok(record["container"], "reassociate")
        self._wait_association(record, initial_frequencies, "initial")
        self.write(record, values)

    def _bsses(self, container):
        # BSS RANGE=ALL MASK=0x6: bssid= and freq= lines, one pair per BSS
        found, bssid = {}, None
        for line in self.control(container, "bss", "RANGE=ALL", "MASK=0x6").splitlines():
            key, _, value = line.partition("=")
            if key == "bssid":
                bssid = value.lower()
            elif key == "freq" and bssid and value.isdigit():
                found[bssid] = int(value)
        return found

    def _scan_passive(self, record, frequencies):
        # REASSOCIATE's own scan is active and short (about 40 ms on one channel): it
        # sees only the APs whose probe responses arrive in time, and the client can
        # join a distant AP while a much stronger one goes unheard. A passive scan
        # listens for a beacon interval per channel and hears every AP in range;
        # REASSOCIATE then selects from its results (fast associate, within 5 s).
        container = record["container"]
        known = self._bsses(container)
        deadline = self.clock() + 3
        request = ("scan", "TYPE=ONLY", "freq=" + ",".join(map(str, sorted(frequencies))), "passive=1")
        while (reply := self.control(container, *request)) != "OK":
            if reply != "FAIL-BUSY" or self.clock() >= deadline:
                raise ActuatorError(f"{container}: passive band scan failed: {reply}")
            self.sleep(0.1)
        # The results enter the BSS table in one update, so the first new BSS on the
        # scanned frequencies marks the scan complete.
        while self.clock() < deadline:
            if any(frequency in frequencies and bssid not in known
                   for bssid, frequency in self._bsses(container).items()):
                return
            self.sleep(0.05)

    def _wait_association(self, record, frequencies, phase):
        deadline = self.clock() + 12
        while self.clock() < deadline:
            status = dict(line.split("=", 1) for line in self.control(record["container"], "status").splitlines()
                          if "=" in line)
            if (status.get("wpa_state") == "COMPLETED" and status.get("ssid") == record["ssid"]
                    and int(status.get("freq", 0)) > 0
                    and (frequencies is None or int(status["freq"]) in frequencies)):
                return
            self.sleep(0.1)
        raise ActuatorError(f"{record['container']}: {phase} band association timed out")


class BandProfileManager:
    def __init__(self, plan, recovery, *, settings=None, scanner=None):
        self.plan = plan
        self.recovery = recovery
        self.settings = settings or ClientBandSettings()
        self.scanner = scanner or NativeBandScanner()
        self.active = {}

    def _prepare(self, role, profile):
        binding = self.plan["bindings"][role]
        container = binding["container"]
        sta_mac = binding.get("station_mac", binding["radio_permanent_mac"]).lower()
        capabilities = self.scanner.capabilities(container)
        if capabilities["sta_mac"] != sta_mac:
            raise ActuatorError("band capability station identity mismatch")
        frequencies = {band: sorted({int(ap.get("fronthaul_frequencies_mhz", {}).get(band, 0))
                                    for ap in self.plan["bindings"].values()} - {0})
                       for band in profile["allowed_bands"]}
        if any(not values or not set(values) <= set(capabilities["frequencies_mhz"]) for values in frequencies.values()):
            raise ActuatorError("requested band is not supported by both client and live AP radios")
        record = self.settings.capture(container, sta_mac)
        methods = record["values"]["key_mgmt"].split()
        if "WPA-PSK" not in capabilities["key_management"]:
            raise ActuatorError("client lacks the lab WPA-PSK capability")
        if "6" in frequencies and "SAE" not in capabilities["key_management"]:
            raise ActuatorError("6 GHz requires native client SAE capability")
        methods = ["WPA-PSK", "SAE"] if "6" in frequencies else [method for method in methods if method in {"WPA-PSK", "SAE"}]
        if not methods:
            raise ActuatorError("client network has no compatible personal-security method")
        all_frequencies = sorted({value for values in frequencies.values() for value in values})
        values = {"freq_list": " ".join(map(str, all_frequencies)), "scan_freq": " ".join(map(str, all_frequencies)),
                  "key_mgmt": " ".join(methods), "ieee80211w": "1" if "6" in frequencies else record["values"]["ieee80211w"],
                  "sae_pwe": "2" if "6" in frequencies else record["values"]["sae_pwe"]}
        return {"role": role, "container": container, "sta_mac": sta_mac, "profile": profile,
                "capabilities": capabilities, "frequencies_mhz": all_frequencies, "settings": values,
                "initial_frequencies_mhz": frequencies[profile["initial_band"]], "record": record}

    @contextmanager
    def transition(self, profiles, *, present_roles=None):
        prepared = {}
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="band-profile") as executor:
            futures = {role: executor.submit(self._prepare, role, profile) for role, profile in profiles.items()}
            for role, future in futures.items():
                prepared[role] = future.result()
        previous = copy.deepcopy(self.active)
        originals = self.recovery.client_networks()
        records = {item["container"]: item["record"] for item in prepared.values()}
        for item in previous.values():
            records.setdefault(item["container"], self.settings.capture(item["container"], item["sta_mac"]))
        for container, record in records.items():
            self.recovery.preserve_client_network(container, record)
        try:
            leaving = {item["container"] for item in previous.values()} - {item["container"] for item in prepared.values()}
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="band-initial-association") as executor:
                restorations = []
                for item in previous.values():
                    if item["container"] in leaving:
                        online = present_roles is None or item["role"] in present_roles
                        restorations.append(executor.submit(self.settings.restore, originals[item["container"]],
                                                            reconnect=online, wait=online))
                for future in restorations:
                    future.result()
                futures = [executor.submit(self.settings.initialize, item["record"], item["settings"],
                                           item["initial_frequencies_mhz"]) for item in prepared.values()]
                for future in futures:
                    future.result()
            self.active = {item["sta_mac"]: {key: value for key, value in item.items() if key != "record"}
                           for item in prepared.values()}
            yield
        except BaseException as error:
            self.active = previous
            failures = []
            for record in records.values():
                try:
                    self.settings.restore(record)
                except Exception as rollback_error:
                    failures.append(str(rollback_error))
            if failures:
                self.recovery.failed("band settings rollback failed: " + "; ".join(failures))
                raise ActuatorError(f"band settings failed: {error}; rollback failed: {failures}") from error
            for container in records.keys() - originals.keys():
                self.recovery.release_client_network(container)
            raise
        else:
            for container in leaving:
                self.recovery.release_client_network(container)

    def snapshot(self):
        return copy.deepcopy(self.active)


def restore_client_network(record):
    ClientBandSettings().restore(record)
