import importlib.util
import json
from pathlib import Path
import threading

import pytest


SOURCE = Path(__file__).with_name("room-feature-guest-audit.py")


@pytest.fixture
def audit():
    spec = importlib.util.spec_from_file_location("band_audit_discovery", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("containers,target", [
    (["wlan-client", "wlan-client-001", "wlan-client-002", "wlan-client-003"], "10.0.0.1"),
    (["prpl-client-01", "prpl-client-02"], "192.168.77.1"),
])
def test_band_discovery_queries_only_selected_clients_in_parallel(audit, monkeypatch, containers, target):
    barrier = threading.Barrier(len(containers))
    processes = {container: 100 + index for index, container in enumerate(containers)}
    calls = []

    def command(*arguments, timeout):
        calls.append((arguments, timeout))
        if arguments[:2] == ("lxc", "query"):
            container = arguments[2].removeprefix("/1.0/instances/").removesuffix("/state")
            assert container in processes
            assert timeout == 20
            barrier.wait(timeout=2)
            return json.dumps({"pid": processes[container], "status": "Running"})
        assert arguments[:2] == ("nsenter", "--target")
        assert int(arguments[2]) in processes.values()
        assert arguments[-2:] == ("band-probe", target)
        assert timeout == 15
        return json.dumps({"stable_owner": True, "traffic_ok": True})

    monkeypatch.setattr(audit, "command", command)
    mapping = {f"client_{index}": container for index, container in enumerate(containers)}
    result = audit.band_links({"mapping": mapping, "target": target})
    assert set(result) == set(mapping)
    assert all(value["stable_owner"] and value["traffic_ok"] for value in result.values())
    assert len(calls) == 2 * len(containers)
    assert all("recursion=" not in " ".join(arguments) for arguments, _ in calls)


@pytest.mark.parametrize("state", [
    {"pid": 55, "status": "Stopped"},
    {"pid": 1, "status": "Running"},
    {"pid": True, "status": "Running"},
    {"pid": "55", "status": "Running"},
    {"status": "Running"},
])
def test_band_discovery_rejects_unavailable_namespaces(audit, monkeypatch, state):
    calls = []

    def command(*arguments, timeout):
        calls.append(arguments)
        return json.dumps(state)

    monkeypatch.setattr(audit, "command", command)
    with pytest.raises(RuntimeError, match="namespace is unavailable"):
        audit.band_links({"mapping": {"client": "wlan-client"}, "target": "10.0.0.1"})
    assert len(calls) == 1
    assert calls[0][:2] == ("lxc", "query")
