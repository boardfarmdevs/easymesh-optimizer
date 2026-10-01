"""The controller stacks the optimizer runs on, and what differs between them.

The policy, planners, verifier and recorder are shared. What differs per stack is
named here once, as easymesh-medium's ``wmdcfg/stacks.py`` does for the medium:
the controller container, the API the observer reads, the unit of the native
byte counters and the steering script. The steering script belongs to the lab
that mounts this repository: the optimizer is checked out as a directory of that
lab (``gen/optimizer`` in meta-cmf-bananapi-vcpe, ``optimizer`` in prplmesh-lab),
next to the medium (``medium``), so the lab is the checkout's parent directory.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Stack:
    name: str
    controller: str
    base_url: str
    byte_counter_unit_bytes: int
    steer_script: str


STACKS = {
    "rdk": Stack(
        name="rdk", controller="bpibroadband", base_url="http://127.0.0.1:8888",
        byte_counter_unit_bytes=1, steer_script="steer.sh",
    ),
    "prplmesh": Stack(
        name="prplmesh", controller="prpl-controller", base_url="http://127.0.0.1:8092",
        byte_counter_unit_bytes=1024, steer_script="scripts/steer-client.sh",
    ),
}

LAB = Path(__file__).resolve().parents[2]


def detect(lab: Path = LAB) -> str | None:
    """The stack of the lab that mounts the optimizer, from its steering script."""
    found = [name for name, stack in STACKS.items() if (lab / stack.steer_script).is_file()]
    return found[0] if len(found) == 1 else None


def stack(name: str | None = None, lab: Path = LAB) -> Stack:
    """A named stack, else OPTIMIZER_STACK, else the mounting lab's."""
    name = name or os.environ.get("OPTIMIZER_STACK") or detect(lab)
    if name not in STACKS:
        raise LookupError(
            f"unknown optimizer stack {name!r}: pass --backend or set OPTIMIZER_STACK "
            f"to one of {', '.join(sorted(STACKS))}"
            if name else
            f"no lab found at {lab}: pass --backend or set OPTIMIZER_STACK "
            f"to one of {', '.join(sorted(STACKS))}"
        )
    return STACKS[name]


def steer_script(stack_: Stack, lab: Path = LAB) -> str:
    return str(lab / stack_.steer_script)
