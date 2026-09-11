from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import re
import subprocess

from wmdcfg.actuator import ActuatorError


@contextmanager
def parallel_disconnections(managers):
    managers = list(managers)
    with ExitStack() as cleanup:
        if managers:
            with ThreadPoolExecutor(max_workers=min(4, len(managers)), thread_name_prefix="client-disconnect") as executor:
                entered = [(manager, executor.submit(manager.__enter__)) for manager in managers]
                failures = []
                for manager, future in entered:
                    try:
                        future.result()
                        cleanup.push(manager)
                    except BaseException as error:
                        failures.append(error)
                if failures:
                    raise failures[0]
        yield


def parallel_reconnections(reconnect, roles):
    roles = list(roles)
    if roles:
        with ThreadPoolExecutor(max_workers=min(4, len(roles)), thread_name_prefix="client-reconnect") as executor:
            list(executor.map(reconnect, roles))


def read_client_link(container: str, bssid: str, band: str, target: str) -> dict | None:
    if not re.fullmatch(r"wlan-client(?:-[0-9]{3})?", container):
        return None
    try:
        probe = subprocess.run(
            ["lxc", "exec", container, "--", "ping", "-I", "wlan0", "-c", "1", "-W", "1", target],
            capture_output=True, text=True, timeout=3,
        )
        if probe.returncode:
            return None
        result = subprocess.run(
            ["lxc", "exec", container, "--", "iw", "dev", "wlan0", "link"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode:
            return None
        owner = re.search(r"^Connected to ([0-9a-fA-F:]{17}) ", result.stdout)
        frequency = re.search(r"\bfreq:\s*(\d+)", result.stdout)
        signal = re.search(r"\bsignal:\s*(-?\d+) dBm", result.stdout)
        if not owner or owner.group(1).lower() != bssid.lower() or not frequency or not signal:
            return None
        frequency_mhz = int(frequency.group(1))
        observed_band = ("2.4" if 2412 <= frequency_mhz <= 2484 else
                         "5" if 5000 <= frequency_mhz < 5955 else
                         "6" if 5955 <= frequency_mhz <= 7115 else None)
        rssi = int(signal.group(1))
        if observed_band != band or not -109 <= rssi <= 0:
            return None
        return {"rcpi": 2 * (rssi + 110),
                "metric_observed_at": datetime.now(timezone.utc).isoformat(),
                "measurement_source": "client_kernel_iw_link_after_wlan_traffic_probe"}
    except (OSError, subprocess.TimeoutExpired):
        return None


def reconnect_client(container: str) -> None:
    _command(container, "reconnect")


def _command(container: str, operation: str) -> None:
    if not re.fullmatch(r"wlan-client(?:-[0-9]{3})?", container):
        raise ActuatorError("Wi-Fi control requires a bound WLAN client container")
    result = subprocess.run(
        ["lxc", "exec", container, "--", "wpa_cli", "-i", "wlan0", operation],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode or result.stdout.strip() != "OK":
        raise ActuatorError(f"could not {operation} {container}: {result.stderr.strip() or result.stdout.strip()}")


@contextmanager
def disconnected_client(plan: dict, role: str, recovery):
    container = plan["bindings"][role]["container"]
    previously_paused = container in recovery.paused_clients()
    recovery.pause_client(container)
    try:
        _command(container, "disconnect")
        yield
    except BaseException:
        if not previously_paused:
            reconnect_client(container)
            recovery.resume_client(container)
        raise


def resume_bound_client(plan: dict, role: str, recovery) -> None:
    container = plan["bindings"][role]["container"]
    if container in recovery.paused_clients():
        reconnect_client(container)
        recovery.resume_client(container)
