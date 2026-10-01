from concurrent.futures import ThreadPoolExecutor
import itertools
import json
import subprocess
import threading

import pytest

from optimizer import lxd_ubus
from optimizer.candidates import CandidateMetricsUnavailable
from optimizer.lxd_ubus import ControllerNamespaceChanged, LxdUbusTransport
from optimizer.prplmesh import PrplMeshCandidateProvider


@pytest.fixture
def native(monkeypatch):
    monkeypatch.setattr(lxd_ubus.os, "geteuid", lambda: 0)
    monkeypatch.setattr(lxd_ubus.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(LxdUbusTransport, "_stamp", staticmethod(lambda process: (12345, "67890")))
    descriptors = itertools.count(20)
    opened, closed, calls = [], [], []

    def open_handle(path, flags):
        descriptor = next(descriptors)
        opened.append((path, flags, descriptor))
        return descriptor

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        value = {"status": "Running", "pid": 321} if arguments[0] == "lxc" else {"retval": ""}
        return subprocess.CompletedProcess(arguments, 0, json.dumps(value))

    monkeypatch.setattr(lxd_ubus.os, "open", open_handle)
    monkeypatch.setattr(lxd_ubus.os, "close", closed.append)
    monkeypatch.setattr(lxd_ubus.subprocess, "run", run)
    return opened, closed, calls, run


def test_namespace_is_discovered_once_and_descriptors_are_pinned_per_call(native):
    opened, closed, calls, _run = native
    transport = LxdUbusTransport("prpl-controller")
    for _attempt in range(2):
        assert json.loads(transport("Network", "Add", {"station": "aa:bb"}).stdout) == {"retval": ""}
    assert transport.name == "controller-mount-namespace"
    assert len(calls) == 3
    assert calls[0][0] == ["lxc", "query", "/1.0/instances/prpl-controller/state"]
    for index, (arguments, options) in enumerate(calls[1:]):
        handles = (20 + index * 2, 21 + index * 2)
        assert arguments == ["nsenter", f"--mount=/proc/self/fd/{handles[0]}",
                             f"--root=/proc/self/fd/{handles[1]}", "--wd=/", "--",
                             "/usr/bin/ubus", "-t", "5", "call", "Network", "Add", '{"station":"aa:bb"}']
        assert options == {"pass_fds": handles, "check": True, "text": True,
                           "capture_output": True, "timeout": 12}
    assert sorted(closed) == sorted(item[2] for item in opened)


def test_namespace_calls_are_not_serialized(native, monkeypatch):
    _opened, closed, calls, run = native
    ready = threading.Barrier(4)

    def concurrent_run(arguments, **kwargs):
        if arguments[0] == "nsenter":
            ready.wait(2)
        return run(arguments, **kwargs)

    monkeypatch.setattr(lxd_ubus.subprocess, "run", concurrent_run)
    transport = LxdUbusTransport("prpl-controller")
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda index: transport("Network", "Get", {"index": index}), range(4)))
    assert sum(arguments[0] == "lxc" for arguments, _options in calls) == 1
    assert len(closed) == len(set(closed)) == 8


def test_controller_restart_fails_closed_without_reusing_registration_identity(native, monkeypatch):
    opened, _closed, calls, _run = native
    transport = LxdUbusTransport("prpl-controller")
    transport("Network", "Get", {})
    monkeypatch.setattr(transport, "_stamp", lambda process: (54321, "98765"))
    for _attempt in range(2):
        with pytest.raises(ControllerNamespaceChanged, match="controller changed"):
            transport("Network", "Get", {})
    assert len(calls) == 2 and len(opened) == 2


def test_process_reuse_during_pinning_closes_handles_without_rpc(native, monkeypatch):
    opened, closed, calls, _run = native
    stamps = iter([(12345, "67890"), (12345, "67890"), (54321, "98765")])
    transport = LxdUbusTransport("prpl-controller")
    monkeypatch.setattr(transport, "_stamp", lambda process: next(stamps))
    with pytest.raises(ControllerNamespaceChanged, match="while pinning"):
        transport("Network", "Get", {})
    assert len(calls) == 1
    assert sorted(closed) == sorted(item[2] for item in opened)


@pytest.mark.parametrize("failure", [PermissionError("denied"), FileNotFoundError("exited")])
def test_partial_pin_failure_closes_handles_without_fallback(native, monkeypatch, failure):
    opened, closed, calls, _run = native
    open_handle = lxd_ubus.os.open

    def failing_open(path, flags):
        if path.endswith("/root"):
            raise failure
        return open_handle(path, flags)

    monkeypatch.setattr(lxd_ubus.os, "open", failing_open)
    with pytest.raises((PermissionError, ControllerNamespaceChanged)):
        LxdUbusTransport("prpl-controller")("Network", "Get", {})
    assert len(calls) == 1
    assert len(opened) == len(closed) == 1


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("ubus", 12), subprocess.CalledProcessError(1, "ubus")])
def test_rpc_failure_releases_handles_and_is_reported_as_unavailable(native, monkeypatch, failure):
    opened, closed, calls, run = native

    def failing_run(arguments, **kwargs):
        if arguments[0] == "nsenter":
            raise failure
        return run(arguments, **kwargs)

    monkeypatch.setattr(lxd_ubus.subprocess, "run", failing_run)
    provider = PrplMeshCandidateProvider()
    with pytest.raises(CandidateMetricsUnavailable):
        provider._call("Network", "Get", {})
    assert len(calls) == 1 and len(closed) == len(opened) == 2
    assert provider.last_raw[0]["error"]
    assert provider.last_raw[0]["transport"] == "controller-mount-namespace"
    assert provider.last_raw[0]["elapsed_ms"] >= 0


@pytest.mark.parametrize("state", [{"pid": 0, "status": "Stopped"}, {"pid": True, "status": "Running"},
                                   {"pid": "321", "status": "Running"}])
def test_invalid_container_state_never_enters_namespace(native, monkeypatch, state):
    opened, _closed, _calls, _run = native
    monkeypatch.setattr(lxd_ubus.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, json.dumps(state)))
    with pytest.raises(ControllerNamespaceChanged, match="not running"):
        LxdUbusTransport("prpl-controller")("Network", "Get", {})
    assert not opened


def test_non_root_lxd_compatibility(native, monkeypatch):
    opened, _closed, calls, _run = native
    monkeypatch.setattr(lxd_ubus.os, "geteuid", lambda: 1000)
    transport = LxdUbusTransport("prpl-controller")
    transport("Network", "Get", {})
    assert transport.name == "lxd-exec"
    assert not opened
    assert calls[0][0] == ["lxc", "exec", "prpl-controller", "--", "ubus", "-t", "5", "call", "Network", "Get", "{}"]


def test_process_stamp_allows_spaces_and_parentheses_in_comm(tmp_path, monkeypatch):
    process = tmp_path / "321"
    process.mkdir()
    process.joinpath("stat").write_text("321 (weird (process) name) S " + "0 " * 18 + "123456 0 0")
    monkeypatch.setattr(lxd_ubus, "Path", lambda path: tmp_path / path.rsplit("/", 1)[1])
    assert LxdUbusTransport._stamp(321) == (process.stat().st_ino, "123456")


@pytest.mark.parametrize("name", ["remote:controller", "../controller", "--help", "", "controller/other"])
def test_invalid_controller_name(name):
    with pytest.raises(ValueError):
        LxdUbusTransport(name)
