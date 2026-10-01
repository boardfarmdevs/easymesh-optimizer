import importlib.util
import json
from pathlib import Path
import socket
import subprocess

import pytest


SPEC = importlib.util.spec_from_file_location("probe", Path(__file__).with_name("backhaul-native-probe.py"))
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)
SOURCE = bytes.fromhex("020000270201")
PEER = bytes.fromhex("020000270101")
TARGET = socket.inet_aton("192.168.77.1")
REPLY = SOURCE + PEER + bytes.fromhex("08060001080006040002") + PEER + TARGET + SOURCE + bytes(4)


def test_arp_reply_proves_gateway_answer_to_this_bridge():
    assert PROBE.arp_reply(REPLY, SOURCE, TARGET)
    assert PROBE.arp_reply(REPLY + bytes(18), SOURCE, TARGET)


@pytest.mark.parametrize("offset", [0, 6, 12, 14, 16, 18, 19, 20, 21, 22, 28, 32, 38])
def test_arp_reply_rejects_other_frames_or_stations(offset):
    packet = bytearray(REPLY)
    packet[offset] ^= 1
    assert not PROBE.arp_reply(bytes(packet), SOURCE, TARGET)


def test_short_frame_cannot_verify_connectivity():
    assert not PROBE.arp_reply(REPLY[:41], SOURCE, TARGET)


def test_namespace_probe_preserves_failure_reason(monkeypatch):
    def run(command, **kwargs):
        if command[0] == "lxc":
            inventory = [{"name": "prpl-controller", "state": {"pid": 123, "status": "Running"}}]
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(inventory))
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="native interface missing\n")

    monkeypatch.setattr(PROBE.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="prpl-controller: native probe failed: native interface missing"):
        PROBE.collect()


@pytest.mark.parametrize("received,reachable", [(REPLY, True), (None, False)])
def test_probe_uses_no_fabricated_ipv4_address(monkeypatch, received, reachable):
    class Channel:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def bind(self, address):
            assert address == ("br-lan", 0)

        def fileno(self):
            return 123

        def send(self, packet):
            assert packet[:6] == b"\xff" * 6
            assert packet[6:12] == packet[22:28] == SOURCE
            assert packet[28:38] == bytes(10)
            assert packet[38:42] == TARGET

        def settimeout(self, value):
            assert 0 < value <= 1

        def recv(self, size):
            if received is None:
                raise socket.timeout()
            return received

    monkeypatch.setattr(PROBE.socket, "socket", lambda *args: Channel())
    monkeypatch.setattr(PROBE.fcntl, "ioctl", lambda *args: bytes(18) + SOURCE)
    assert PROBE.arp_probe() is reachable
