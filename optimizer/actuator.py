from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import os
import json
import subprocess
import time
import urllib.error
import urllib.request
from typing import Callable, Sequence

from .model import Snapshot, normalize_mac
from .policy import Decision


# CompletedProcess was not subscriptable on the Python 3.8 build host. Keep
# the alias importable there; field-level annotations still document strings.
Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class ActionResult:
    success: bool
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    coordination: dict | None = None

    def to_dict(self):
        return asdict(self)


class SteerActuator:
    def __init__(
        self,
        script: str | Path,
        *,
        request_only: bool = False,
        preview_seconds: float | None = None,
        timeout_seconds: float | None = None,
        runner: Runner | None = None,
    ) -> None:
        self.script = str(Path(script))
        self.request_only = request_only
        if preview_seconds is not None and preview_seconds < 0:
            raise ValueError("preview_seconds cannot be negative")
        self.preview_seconds = preview_seconds
        self.timeout_seconds = timeout_seconds
        self.runner = runner or subprocess.run

    def build_command(self, sta_mac: str, target_bssid: str) -> tuple[str, ...]:
        prefix = (self.script, "--request-only") if self.request_only else (self.script,)
        return (*prefix, normalize_mac(sta_mac), normalize_mac(target_bssid))

    def command_for_decision(self, decision: Decision) -> tuple[str, ...]:
        return self.build_command(decision.sta_mac, decision.target_bssid)

    def execute(self, decision: Decision, snapshot: Snapshot) -> ActionResult:
        if decision.action != "steer" or decision.target_bssid is None:
            raise ValueError("actuator requires a steer decision with a target")
        client = snapshot.client(decision.sta_mac)
        if client is None:
            raise ValueError("STA disappeared before actuation")
        if client.connected_bssid != decision.source_bssid:
            raise ValueError("STA source changed before actuation")
        candidates = {
            item.bssid: item
            for item in snapshot.candidates_for(decision.sta_mac)
            if item.eligible
        }
        if decision.target_bssid not in candidates:
            raise ValueError("target is no longer an eligible observed candidate")
        command = self.command_for_decision(decision)
        completed = self.runner(
            command,
            text=True,
            capture_output=True,
            check=False,
            **({"timeout": self.timeout_seconds} if self.timeout_seconds is not None else {}),
            **({"env": {**os.environ, "EASYMESH_STEERING_PREVIEW_SECONDS": str(self.preview_seconds)}}
               if self.preview_seconds is not None else {}),
        )
        return ActionResult(
            success=completed.returncode == 0,
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            coordination=getattr(completed, "coordination", None),
        )


class NativeSteerActuator(SteerActuator):
    """Submit one gentle native BTM without RF rewriting, scanning or retries."""

    def __init__(self, bss_channels: dict[str, int], *, runner: Runner | None = None, base_url: str | None = None):
        super().__init__("/usr/bin/steer.sh", runner=runner, timeout_seconds=15)
        self.bss_channels = {normalize_mac(address): channel for address, channel in bss_channels.items()}
        self.native_endpoint = base_url.rstrip("/") + "/api/v1/steer-native" if base_url else None
        if self.native_endpoint:
            self.runner = self._submit_http

    def _submit_http(self, command, **_options):
        started = time.monotonic()
        station, target, opclass, channel, _mode, source = command[-6:]
        payload = {"station": station, "source_bssid": source, "target_bssid": target,
                   "operating_class": int(opclass), "channel": int(channel)}
        request = urllib.request.Request(self.native_endpoint, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            try:
                response = urllib.request.urlopen(request, timeout=20)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                value = json.load(response)
                successful = response.status == 200 and value.get("success") is True and value.get("returncode") == 0
                completed = subprocess.CompletedProcess(command, 0 if successful else 1,
                    str(value.get("stdout") or ""), str(value.get("stderr") or value.get("message") or ""))
                completed.coordination = {"transport": "controller-local-helper", "attempts": 1,
                    "server_elapsed_ms": value.get("elapsed_ms"),
                    "round_trip_ms": round((time.monotonic() - started) * 1000, 3)}
                return completed
        except (OSError, ValueError, TypeError, AttributeError) as error:
            return subprocess.CompletedProcess(command, 1, "", str(error))

    def execute(self, decision: Decision, snapshot: Snapshot) -> ActionResult:
        result = super().execute(decision, snapshot)
        statuses = [line.removeprefix("steer_drv_status=") for line in result.stdout.splitlines()
                    if line.startswith("steer_drv_status=")]
        status = statuses[0] if len(statuses) == 1 else "native_command_status_unavailable"
        return ActionResult(result.success and status == "Success", result.command, result.returncode,
                            result.stdout, result.stderr if status == "Success" else result.stderr + "\n" + status,
                            result.coordination)

    def command_for_decision(self, decision: Decision) -> tuple[str, ...]:
        from .candidates import operating_class

        target = normalize_mac(decision.target_bssid or "")
        channel = self.bss_channels[target]
        opclass = operating_class(decision.target_band, channel)
        prefix = ("POST", self.native_endpoint) if self.native_endpoint else ("lxc", "exec", "bpibroadband", "--", self.script)
        return (*prefix,
                normalize_mac(decision.sta_mac), target, str(opclass), str(channel), "gentle",
                normalize_mac(decision.source_bssid))
