"""The lab this room service runs in, and what differs between the labs' stacks.

The room service is checked out with the optimizer in a lab (``gen/optimizer`` in
meta-cmf-bananapi-vcpe, ``optimizer`` in prplmesh-lab), next to the medium
(``medium``) and the lab's rooms (``rooms``: the launcher, the manifests and the role
bindings). The optimizer's stacks (``optimizer/stacks.py``) name the controller, its
API and the steering script; the medium's (``wmdcfg/stacks.py``) the control socket
and the client containers. What the room service itself does differently per stack
is here.

The stack is the named one, else ``OPTIMIZER_STACK``, else the lab's; without a lab
(the tests, an offline replay) the RDK stack's.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from optimizer import stacks

LAB = stacks.LAB
MEDIUM = LAB / "medium"
CONFIGURATOR = MEDIUM / "configurator"
ROOMS = LAB / "rooms"


@dataclass(frozen=True)
class RoomStack:
    name: str
    # the lab repository's root, relative to LAB: manifests name their files from it
    repository: str
    # the room service's unit: its lock, crash-recovery record and default run directory
    service: str
    # controller topology names for nodes no BSSID identifies
    device_roles: dict
    # clients in one streaming candidate round (RDK: OneWifi measures 64 stations per
    # query since ccsp-one-wifi 0040; prplMesh's rounds were qualified with 8)
    candidate_round_clients: int
    # refresh only the candidates about to expire (RDK) or every candidate each round
    refresh_candidates: bool
    # steer verifications watched at once while profiling
    verifications: int
    # profiling steers through the controller's native endpoint (RDK's em_cli)
    native_steering: bool
    # a client's AP by its room name before the controller's name for it (prplMesh
    # names its nodes agent-N, not the room's Extender-N)
    world_names_first: bool
    # label a client by its station number (sta-NN, iot-NN), as prplMesh's controller
    # UI does; the RDK lab's viewer labels clients by their room role
    station_labels: bool
    # an AP on a wired backhaul has RF to the other APs only with a HAL guard that never
    # connects its backhaul station (RDK: rdk-wifi-hal 0045, wired_guard=hal); prplMesh's
    # wired Agent has no backhaul station at all (scripts/radio-lab.sh)
    wired_rf_needs_guard: bool
    # the RDK OneWifi backhaul radios: prepared for geometry rooms, adaptive parents
    backhaul_control: bool
    # the patch series an RF capability report fingerprints, relative to the lab's repository
    patch_folders: tuple = ()


ROOM_STACKS = {
    "rdk": RoomStack(
        name="rdk",
        repository="..",
        service="easymesh-room-service",
        device_roles={
            "agent-1": "gateway",
            "extender-1": "extender_1",
            "extender-2": "extender_2",
            "extender-3": "extender_3",
            "extender-4": "extender_4",
        },
        candidate_round_clients=64,
        refresh_candidates=True,
        verifications=16,
        native_steering=True,
        world_names_first=False,
        station_labels=False,
        wired_rf_needs_guard=True,
        backhaul_control=True,
        patch_folders=("gen/medium/hwsim/patches", "gen/medium/wmediumd/patches",
                       "recipes-ccsp/hal/rdk-wifi-hal", "recipes-ccsp/unified-wifi-mesh/unified-wifi-mesh"),
    ),
    "prplmesh": RoomStack(
        name="prplmesh",
        repository=".",
        service="prplmesh-room-service",
        device_roles={
            "controller": "gateway",
            "agent-1": "extender_1",
            "agent-2": "extender_2",
            "agent-3": "extender_3",
            "agent-4": "extender_4",
        },
        candidate_round_clients=8,
        refresh_candidates=False,
        verifications=5,
        native_steering=False,
        world_names_first=True,
        station_labels=True,
        wired_rf_needs_guard=False,
        backhaul_control=False,
        patch_folders=("medium/hwsim/patches", "medium/wmediumd/patches", "patches/prplmesh"),
    ),
}


def room_stack(name: str | None = None) -> RoomStack:
    try:
        return ROOM_STACKS[stacks.stack(name).name]
    except LookupError:
        if name:
            raise
        return ROOM_STACKS["rdk"]


def optimizer_stack(stack: RoomStack) -> stacks.Stack:
    return stacks.STACKS[stack.name]


def repository(stack: RoomStack | None = None) -> Path:
    return (LAB / (stack or room_stack()).repository).resolve()


def control_socket(stack: RoomStack) -> str:
    from wmdcfg.stacks import STACKS
    return STACKS[stack.name].control_socket


def is_client_container(name: str) -> bool:
    """A room client's container in either lab (wlan-client-NNN, prpl-client-NN)."""
    from wmdcfg.stacks import STACKS
    return any(item.client.fullmatch(name) for item in STACKS.values())


def topology_urls() -> set[str]:
    from wmdcfg.stacks import STACKS
    return {item.topology_url for item in STACKS.values()}
