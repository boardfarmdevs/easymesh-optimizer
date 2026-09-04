from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import subprocess
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

    def to_dict(self):
        return asdict(self)


class SteerActuator:
    def __init__(
        self,
        script: str | Path,
        *,
        request_only: bool = False,
        runner: Runner | None = None,
    ) -> None:
        self.script = str(Path(script))
        self.request_only = request_only
        self.runner = runner or subprocess.run

    def build_command(self, sta_mac: str, target_bssid: str) -> tuple[str, ...]:
        prefix = (self.script, "--request-only") if self.request_only else (self.script,)
        return (*prefix, normalize_mac(sta_mac), normalize_mac(target_bssid))

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
        command = self.build_command(decision.sta_mac, decision.target_bssid)
        completed = self.runner(
            command,
            text=True,
            capture_output=True,
            check=False,
        )
        return ActionResult(
            success=completed.returncode == 0,
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
