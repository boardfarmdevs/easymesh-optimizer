from pathlib import Path

import pytest

from optimizer import stacks
from room_service import lab


def test_the_stack_is_the_named_one_else_the_environments_else_rdk_without_a_lab(monkeypatch):
    monkeypatch.setenv("OPTIMIZER_STACK", "prplmesh")
    assert lab.room_stack().name == "prplmesh"
    assert lab.room_stack("rdk").name == "rdk"
    monkeypatch.delenv("OPTIMIZER_STACK")
    monkeypatch.setattr(stacks, "detect", lambda lab=None: None)
    assert lab.room_stack().name == "rdk"
    with pytest.raises(LookupError):
        lab.room_stack("openwrt")


def test_paths_follow_the_mounting_lab():
    root = Path(lab.__file__).resolve().parents[2]
    assert lab.LAB == root
    assert lab.CONFIGURATOR == root / "medium" / "configurator"
    assert lab.ROOMS == root / "rooms"
    # the RDK lab mounts the optimizer in gen/: its repository is one up
    assert lab.repository(lab.ROOM_STACKS["rdk"]) == root.parent.resolve()
    assert lab.repository(lab.ROOM_STACKS["prplmesh"]) == root.resolve()


def test_each_stack_names_its_room_service_and_control_socket():
    rdk, prplmesh = lab.ROOM_STACKS["rdk"], lab.ROOM_STACKS["prplmesh"]
    assert (rdk.service, lab.control_socket(rdk)) == ("easymesh-room-service", "/run/wmediumd-control.sock")
    assert (prplmesh.service, lab.control_socket(prplmesh)) == ("prplmesh-room-service", "/run/prpl-wmediumd/control.sock")
    assert set(lab.ROOM_STACKS) == set(stacks.STACKS)
