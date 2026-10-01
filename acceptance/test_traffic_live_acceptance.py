import copy
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


SPEC = importlib.util.spec_from_file_location("traffic_live", Path(__file__).with_name("traffic-live-acceptance.py"))
DRIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRIVER)


@pytest.fixture
def loss_case():
    phase = {"state": "completed", "source": "bound_client_wlan0_udp_iperf3", "source_interface": "wlan0",
        "port": 55204, "payload_bytes": 1200,
        "sender": {"status": "complete", "source": "iperf3_sender_json", "returncode": 0,
            "packets": 100, "bytes": 120000, "seconds": 2, "bits_per_second": 480000},
        "receiver": {"status": "complete", "source": "iperf3_receiver_json", "returncode": 0,
            "packets": 100, "bytes": 96000, "seconds": 2, "goodput_bits_per_second": 384000,
            "lost_packets": 20, "loss_percent": 20}}
    return [phase], {"sent": 100, "arrived": 100, "dropped": 20, "delivered": 80}


def test_nonzero_loss_requires_independent_counts_and_labels_injection(loss_case):
    phases, counters = loss_case
    result = DRIVER.verify_loss(phases, counters, 1200)
    assert result["passed"] and result["rf_loss_claim"] is False
    assert result["independent_missing_packets"] == 20
    assert result["unobserved_terminal_packets"] == 0


@pytest.mark.parametrize("field,value", [("sent", 101), ("delivered", 81), ("dropped", 0), ("arrived", 99)])
def test_self_consistent_iperf_telemetry_cannot_override_independent_evidence(loss_case, field, value):
    phases, counters = loss_case
    with pytest.raises(RuntimeError):
        DRIVER.verify_loss(phases, {**counters, field: value}, 1200)


@pytest.mark.parametrize("field,value", [("status", "partial"), ("returncode", 1), ("lost_packets", 0),
    ("loss_percent", 0), ("goodput_bits_per_second", 8000000), ("source", "iperf3_sender_json")])
def test_nonzero_loss_does_not_accept_partial_zero_loss_or_requested_goodput(loss_case, field, value):
    phases, counters = loss_case
    phases[0]["receiver"][field] = value
    with pytest.raises(RuntimeError):
        DRIVER.verify_loss(phases, counters, 1200)


def test_terminal_sequence_gap_is_explicit_and_bounded(loss_case):
    phases, counters = loss_case
    phases[0]["receiver"].update(packets=99, lost_packets=19, loss_percent=100 * 19 / 99)
    result = DRIVER.verify_loss(phases, counters, 1200)
    assert result["unobserved_terminal_packets"] == 1 and result["maximum_terminal_packets"] == 1
    phases[0]["receiver"].update(packets=98, lost_packets=18, loss_percent=100 * 18 / 98)
    with pytest.raises(RuntimeError, match="terminal datagram"):
        DRIVER.verify_loss(phases, counters, 1200)


@pytest.mark.parametrize("case", DRIVER.CASES)
def test_preparation_never_contacts_or_mutates_lab(monkeypatch, tmp_path, capsys, case):
    def forbidden(*_args, **_kwargs):
        pytest.fail("preparation attempted live access")
    monkeypatch.setattr(DRIVER.subprocess, "run", forbidden)
    monkeypatch.setattr(DRIVER.urllib.request, "urlopen", forbidden)
    output = tmp_path / "evidence"
    assert DRIVER.main(["--stack", "prpl", "--case", case, "--root", "/unused", "--output", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["live"] is False
    assert not output.exists()


def test_restart_wait_retries_unavailable_api_without_renewing_lease(monkeypatch):
    lab = DRIVER.Lab(SimpleNamespace(stack="prpl"))
    monkeypatch.setattr(lab, "state", Mock(side_effect=[DRIVER.URLError("not listening"), {"enabled": True}]))
    monkeypatch.setattr(lab, "renew", lambda: pytest.fail("restart renewed stale lease"))
    monkeypatch.setattr(DRIVER.time, "sleep", lambda _seconds: None)
    assert lab.wait(lambda state: state.get("enabled"), 1, reconnecting=True) == {"enabled": True}


def test_shutdown_failure_before_stop_does_not_restart_service_or_lose_lease(monkeypatch, tmp_path):
    # the lab the driver reads: the room's bindings and the room service's sources it records
    root = tmp_path / "lab"
    (root / "rooms/bindings").mkdir(parents=True)
    (root / "rooms/bindings/private-client-room-walk.json").write_text('{"roles": {"sta_static_01": "prpl-client-01"}}')
    for name in ("optimizer/room_service/traffic_experiment.py", "optimizer/room_service/interactions.py",
                 "optimizer/optimizer/load_policy.py"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("")
    args = SimpleNamespace(stack="prpl", case="shutdown", root=root, output=tmp_path / "evidence")
    lab = DRIVER.Lab(args)
    initial = {"enabled": True, "lease": {"held": False}, "recording": {"active": False},
        "playback": {"status": "paused", "manual_roles": [], "time_ms": 0}, "roles": {},
        "selected_world": "home-five-agent--private-client-room-walk"}
    def acquire():
        lab.token = "owned-token"
        return {}
    monkeypatch.setattr(DRIVER, "Lab", lambda _args: lab)
    monkeypatch.setattr(lab, "state", lambda: initial)
    monkeypatch.setattr(lab, "audit", lambda _container: {"processes": [], "endpoints": {}})
    monkeypatch.setattr(lab, "acquire", acquire)
    monkeypatch.setattr(lab, "renew", Mock())
    mutate = Mock()
    request = Mock()
    monkeypatch.setattr(lab, "mutate", mutate)
    monkeypatch.setattr(lab, "request", request)
    monkeypatch.setattr(lab, "lifecycle", Mock(side_effect=RuntimeError("UDP setup failed")))
    monkeypatch.setattr(DRIVER, "command", lambda *_args, **_kwargs: pytest.fail("unrequested service restart"))
    result = DRIVER.run(args)
    assert not result["passed"] and result["error"] == "UDP setup failed"
    assert not result["restoration_errors"]
    assert mutate.call_args.args == ("world/apply", {"world": "default"})
    assert request.call_args.args[0] == "interactions/lease" and request.call_args.args[2] == "DELETE"


@pytest.mark.parametrize("failure", [None, "before-insert", "after-insert"])
def test_loss_injection_counts_normalized_rules_and_restores_only_owned_resources(monkeypatch, failure):
    lab = DRIVER.Lab(SimpleNamespace(stack="prpl"))
    tables = {"client": {"OUTPUT": [["-p", "udp", "-j", "ACCEPT"]]}, "prpl-controller": {"INPUT": []}}
    baseline = copy.deepcopy(tables)
    failed = False
    def execute(node, *arguments, check=True):
        nonlocal failed
        stdout, stderr, returncode = "", "", 0
        if arguments[0] == "ip":
            stdout = json.dumps([{"dev": "wlan0"}] if "route" in arguments else
                                [{"addr_info": [{"local": "192.168.77.101", "scope": "global"}]}])
        elif arguments[0] == "iptables-legacy-save":
            lines = []
            for chain, rules in tables[node].items():
                for rule in rules:
                    count = 100 if chain == "OUTPUT" or chain == "INPUT" else 20 if rule[-1] == "DROP" else 80
                    normalized = list(rule)
                    if "udp" in normalized:
                        normalized[normalized.index("udp") + 1:normalized.index("udp") + 1] = ["-m", "udp"]
                    lines.append("[" + str(count) + ":120000] " + shlex.join(["-A", chain, *normalized]))
            stdout = "\n".join(lines)
        else:
            operation, *values = arguments[5:]
            if operation == "-S":
                stdout = "\n".join("-N " + chain for chain in tables[node])
            else:
                chain, *rule = values
                if operation == "-N":
                    tables[node][chain] = []
                elif operation == "-X":
                    assert not tables[node][chain]
                    del tables[node][chain]
                elif operation == "-F":
                    tables[node][chain].clear()
                elif operation in ("-I", "-A"):
                    inject_failure = failure and not failed and operation == "-I" and chain == "OUTPUT"
                    if inject_failure and failure == "before-insert":
                        failed = True
                        raise subprocess.TimeoutExpired(arguments, 3)
                    tables[node][chain].append(rule)
                    if inject_failure:
                        failed = True
                        raise subprocess.TimeoutExpired(arguments, 3)
                elif operation == "-D":
                    tables[node][chain].remove(rule)
                elif operation == "-C":
                    returncode = 0 if rule in tables[node][chain] else 1
                else:
                    pytest.fail("unexpected firewall operation")
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)
    @contextmanager
    def namespace(node):
        yield lambda *arguments, **options: execute(node, *arguments, **options)
    monkeypatch.setattr(lab, "namespace", namespace)
    try:
        with lab.loss_counters("client", 1200) as read:
            evidence = read()
            assert evidence["counters"] == {"sent": 100, "arrived": 100, "dropped": 20, "delivered": 80}
            assert "--dport 55204" in evidence["raw"]["sender"]
            assert "--length 1228" in evidence["raw"]["sender"]
            assert "-s 192.168.77.101/32 -d 192.168.77.1/32" in evidence["raw"]["sender"]
    except subprocess.TimeoutExpired:
        assert failure is not None
    assert not lab.cleanup_errors and tables == baseline
