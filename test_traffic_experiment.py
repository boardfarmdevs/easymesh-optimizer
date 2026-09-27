import copy
import json
import signal
import subprocess
import time
from contextlib import contextmanager, nullcontext
from io import StringIO
from threading import Event, RLock
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from room_demo.traffic_experiment import (TrafficCancelled, TrafficExperiment, UDP_PORT, namespace_ping,
                                          namespace_udp, packet_counts, phase_at, udp_endpoint_record)
from wmdcfg.traffic_profile import validate_traffic


PHASE = {"role": "client", "start_ms": 5000, "end_ms": 10000,
         "packets_per_second": 10, "payload_bytes": 1200}
WORLD = {"roles": {"client": "station"}, "generations": [{"present": {"client": True}}],
         "duration_ms": 30000,
         "traffic_experiment": {"schema": "easymesh.room-traffic.v1", "phases": [PHASE]}}
UDP_PHASE = {"role": "client", "start_ms": 5000, "end_ms": 10000,
             "mode": "udp", "offered_mbps": 8, "payload_bytes": 1200}


def udp_output(sender, *, legacy=False):
    summary = {"sender": sender, "bytes": 12000 if sender else 9600, "packets": 10,
               "seconds": 2, "bits_per_second": 48000 if sender else 38400}
    if not sender:
        summary.update(lost_packets=2, lost_percent=20)
    return {"start": {"test_start": {"protocol": "UDP", "num_streams": 1, "reverse": 0}},
            "intervals": [{"sum": {**summary, "start": 0, "end": 2, "omitted": False}}],
            "end": ({"sum": {**summary, "bytes": 999999, "bits_per_second": 999999}}
                    if legacy else {"sum_sent" if sender else "sum_received": summary})}


@pytest.mark.parametrize("legacy", [True, False])
def test_udp_uses_each_endpoint_measurements_in_39_and_312_formats(legacy):
    sender = udp_endpoint_record(json.dumps(udp_output(True, legacy=legacy)), True)
    receiver = udp_endpoint_record(json.dumps(udp_output(False, legacy=legacy)), False)
    assert sender["status"] == receiver["status"] == "complete"
    assert sender["bits_per_second"] == 48000
    assert receiver["goodput_bits_per_second"] == 38400
    assert receiver["lost_packets"] == 2 and receiver["loss_percent"] == 20
    assert receiver["bytes"] == 9600
    assert sender["source"] != receiver["source"]


@pytest.mark.parametrize("change", [
    {"bits_per_second": None}, {"bits_per_second": float("nan")}, {"bytes": True},
    {"seconds": 0}, {"packets": -1}, {"sender": True}, {"lost_packets": 11},
    {"lost_percent": 110}, {"lost_percent": 0},
    {"bits_per_second": 8000000},
])
def test_udp_invalid_receiver_records_never_become_zero_or_offered_goodput(change):
    output = udp_output(False)
    output["end"]["sum_received"].update(change)
    record = udp_endpoint_record(json.dumps(output), False)
    assert record["status"] == "invalid"
    assert record["goodput_bits_per_second"] is None and record["loss_percent"] is None


def test_udp_missing_truncated_partial_and_zero_loss_records_are_explicit():
    assert udp_endpoint_record("", False)["status"] == "missing"
    assert udp_endpoint_record('{"end":', False)["status"] == "invalid"
    output = udp_output(False)
    output["error"] = "interrupted"
    assert udp_endpoint_record(json.dumps(output), False)["status"] == "partial"
    output.pop("error")
    output["end"]["sum_received"].update(lost_packets=0, lost_percent=0, bytes=0, bits_per_second=0)
    record = udp_endpoint_record(json.dumps(output), False)
    assert record["status"] == "complete" and record["loss_percent"] == record["goodput_bits_per_second"] == 0
    output["start"]["test_start"]["protocol"] = "TCP"
    assert udp_endpoint_record(json.dumps(output), False)["status"] == "invalid"


@pytest.fixture
def udp_runtime(monkeypatch):
    from room_demo import traffic_experiment as module
    state = SimpleNamespace(calls=[], processes=[], rules=[], closed=[], clock=100.0, legacy=False,
                            route="wlan0", source="192.168.77.2", target="192.168.77.1", bridge="br-lan",
                            addresses=None, fail=None, registered=[], launched=[], report=None)
    monkeypatch.setattr(module, "os", SimpleNamespace(**vars(module.os)))
    monkeypatch.setattr(module, "time", SimpleNamespace(**vars(module.time)))
    monkeypatch.setattr(module, "tempfile", SimpleNamespace(TemporaryFile=lambda **_kwargs: StringIO()))
    monkeypatch.setattr(module.time, "monotonic", lambda: state.clock)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: setattr(state, "clock", state.clock + seconds))
    monkeypatch.setattr(module.os, "stat", lambda _path: SimpleNamespace(st_ino=1))
    monkeypatch.setattr(module.os, "fstat", lambda descriptor: SimpleNamespace(st_ino=descriptor))
    monkeypatch.setattr(module.os, "open", lambda path, *_args: 9 if "/42/" in path else 10 if "/43/" in path else 11)
    monkeypatch.setattr(module.os, "close", state.closed.append)
    monkeypatch.setattr(module.fcntl, "flock", Mock())

    def execute(arguments, **options):
        state.calls.append((arguments, options))
        if state.fail:
            state.fail(arguments)
        descriptor = None
        if arguments[0] == "nsenter":
            descriptor = int(arguments[1].rsplit("/", 1)[1])
            assert options["pass_fds"] == (descriptor,)
            arguments = arguments[3:]
        output, returncode = "", 0
        if arguments[0] == "lxc":
            output = json.dumps({"status": "Running", "pid": 43 if "controller" in arguments[-1]
                                 or "bpibroadband" in arguments[-1] else 42})
        elif arguments[0] == "ip" and "address" in arguments:
            output = json.dumps(state.addresses if state.addresses is not None and descriptor == 9 else
                                [{"ifname": "wlan0" if descriptor == 9 else state.bridge,
                                  "addr_info": [{"family": "inet", "scope": "global",
                                                 "local": state.source if descriptor == 9 else state.target}]}])
        elif arguments[0] == "ip":
            output = json.dumps([{"dev": state.route}])
        elif arguments[0] == "iperf3":
            output = "iperf3 3.9" if state.legacy else "iperf3 3.12 --bind-dev"
        elif arguments[0] == "ss" and "-lntp" in arguments:
            output = f'LISTEN 0 1 {state.target}:{UDP_PORT} *:* users:(("iperf3",pid={state.processes[0].pid},fd=3))'
        elif arguments[0] == "iptables-legacy":
            operation = arguments[3]
            rule = (descriptor, *arguments[4:])
            if operation == "-I":
                state.rules.append(rule)
            elif operation == "-D":
                state.rules.remove(rule)
            elif operation == "-C":
                returncode = 0 if rule in state.rules else 1
            else:
                pytest.fail("unexpected firewall operation")
        return SimpleNamespace(stdout=output, stderr="", returncode=returncode)

    def launch(arguments, **options):
        state.launched.append(arguments)
        if state.fail:
            state.fail(arguments)
        process = Mock(pid=100 + len(state.processes), returncode=None)
        process.poll.side_effect = lambda: process.returncode
        process.wait.side_effect = lambda **_kwargs: setattr(process, "returncode", 130 if process.returncode is None else process.returncode)
        options["stdout"].write(json.dumps(udp_output("-c" in arguments, legacy=state.legacy)))
        options["stdout"].flush()
        state.processes.append(process)
        if len(state.processes) == 2:
            for child in state.processes:
                child.returncode = 0
        assert options["pass_fds"] == ((9,) if "-c" in arguments else (10,))
        assert options["start_new_session"] is True
        return process

    def signal_process(process_pid, _signum):
        next(process for process in state.processes if process.pid == process_pid).returncode = 130

    monkeypatch.setattr(module.subprocess, "run", execute)
    monkeypatch.setattr(module.subprocess, "Popen", launch)
    monkeypatch.setattr(module.os, "killpg", signal_process)
    return state


def run_udp(state, *, admit=nullcontext, running=None):
    return namespace_udp("wlan-client" if state.bridge == "brlan0" else "prpl-client-01", state.target,
                         UDP_PHASE, 5000, admit=admit, register=state.registered.append,
                         running=running or (lambda value: setattr(state, "report", copy.deepcopy(value))))


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("rdk", [False, True])
def test_udp_pins_both_namespaces_bounds_processes_and_cleans_only_owned_rules(udp_runtime, legacy, rdk):
    state = udp_runtime
    state.legacy = legacy
    if rdk:
        state.bridge, state.source, state.target = "brlan0", "10.0.0.2", "10.0.0.1"
    result = run_udp(state)
    assert result["state"] == "completed"
    assert result["sender"]["bits_per_second"] == 48000
    assert result["receiver"]["goodput_bits_per_second"] == 38400
    assert state.report["receiver"]["status"] == "missing"
    assert len(state.processes) == len(state.registered) == 2
    sender = state.launched[1]
    assert sender[sender.index("-b") + 1] == "8000000"
    assert sender[sender.index("-B") + 1] == state.source
    assert sender[sender.index("-c") + 1] == state.target
    assert 1 <= int(sender[sender.index("-t") + 1]) <= 4
    assert sender[sender.index("-P") + 1] == "1"
    assert "--bind-dev" in sender if not legacy else "--bind-dev" not in sender
    insertions = [arguments for arguments, _options in state.calls if "-I" in arguments]
    deletions = [arguments for arguments, _options in state.calls if "-D" in arguments]
    assert len(insertions) == len(deletions) == 2 * (legacy + rdk)
    assert not state.rules and sorted(state.closed) == [9, 10, 11]
    for arguments in insertions:
        assert arguments[arguments.index("-s") + 1] == state.source + "/32"
        assert arguments[arguments.index("-d") + 1] == state.target + "/32"
        assert arguments[arguments.index("--dport") + 1] == str(UDP_PORT)
        assert "--comment" in arguments
        if "OUTPUT" in arguments:
            assert arguments[-5:] == ["!", "-o", "wlan0", "-j", "REJECT"]
        else:
            assert arguments[-4:] == ["-i", "brlan0", "-j", "ACCEPT"]


@pytest.mark.parametrize("route", ["lo", "eth0", "eth1"])
def test_udp_rejects_management_and_loopback_routes_before_any_mutation(udp_runtime, route):
    udp_runtime.route = route
    result = run_udp(udp_runtime)
    assert result["state"] == "failed" and "bypass" in result["error"]
    assert not udp_runtime.launched and not udp_runtime.rules


def test_udp_refuses_private_address_on_gateway_management_interface(udp_runtime):
    udp_runtime.bridge = "eth0"
    result = run_udp(udp_runtime)
    assert result["state"] == "failed" and "LAN bridge" in result["error"]
    assert not udp_runtime.launched


def test_udp_refuses_client_local_gateway_target(udp_runtime):
    udp_runtime.source = udp_runtime.target
    result = run_udp(udp_runtime)
    assert result["state"] == "failed" and "nonlocal" in result["error"]
    assert not udp_runtime.launched


@pytest.mark.parametrize("failure", ["second-rule", "sender-launch", "cancel-after-server"])
def test_udp_partial_setup_failure_reaps_and_removes_scoped_allowances(udp_runtime, failure):
    state = udp_runtime
    state.bridge, state.source, state.target = "brlan0", "10.0.0.2", "10.0.0.1"
    def fail(arguments):
        if failure == "second-rule" and "-I" in arguments and "udp" in arguments:
            raise subprocess.CalledProcessError(1, arguments)
        if failure == "sender-launch" and "-c" in arguments:
            raise OSError("cannot launch client")
        if failure == "cancel-after-server" and "-lntp" in arguments:
            raise TrafficCancelled("playback generation revoked")
    state.fail = fail
    result = run_udp(state)
    assert result["state"] == ("cancelled" if failure.startswith("cancel") else "failed")
    assert result["receiver"]["status"] == ("missing" if failure == "second-rule" else "partial")
    if failure != "second-rule":
        assert result["receiver"]["goodput_bits_per_second"] == 38400
    assert not state.rules and sorted(state.closed) == [9, 10, 11]
    assert all(process.returncode is not None for process in state.processes)


def test_udp_revoked_admission_starts_no_commands(udp_runtime):
    def admit():
        raise TrafficCancelled("revoked")
    assert run_udp(udp_runtime, admit=admit)["state"] == "cancelled"
    assert not udp_runtime.calls and not udp_runtime.launched


@pytest.mark.parametrize("inserted", [False, True])
def test_udp_cancellation_around_rule_insertion_has_no_false_cleanup_failure(udp_runtime, inserted):
    state = udp_runtime
    state.bridge, state.source, state.target = "brlan0", "10.0.0.2", "10.0.0.1"
    revoked = False
    @contextmanager
    def admit():
        if revoked:
            raise TrafficCancelled("revoked")
        yield
    def fail(arguments):
        nonlocal revoked
        if "-I" in arguments:
            revoked = True
            if not inserted:
                raise TrafficCancelled("revoked before insertion")
    state.fail = fail
    result = run_udp(state, admit=admit)
    assert result["state"] == "cancelled" and "cleanup_errors" not in result
    assert not state.rules and not state.launched
    assert sum("-D" in arguments for arguments, _options in state.calls) == int(inserted)


def test_udp_failed_rule_cleanup_is_explicit_and_disables_later_udp_runs(udp_runtime):
    state = udp_runtime
    state.bridge, state.source, state.target = "brlan0", "10.0.0.2", "10.0.0.1"
    def fail(arguments):
        if "-D" in arguments:
            raise subprocess.CalledProcessError(1, arguments)
    state.fail = fail
    result = run_udp(state)
    assert result["state"] == "failed" and len(result["cleanup_errors"]) == 2
    runner = Mock(return_value=result)
    actor = TrafficExperiment(state.target, Mock(), udp_runner=runner)
    job = {"key": ("room", 0, 0), "phase": UDP_PHASE, "container": "wlan-client", "deadline": state.clock + 5}
    actor._udp_job(job, 0)
    actor._udp_job(job, 0)
    assert runner.call_count == 1
    assert "operator cleanup required" in actor.snapshot()["error"]
    actor.close()


def test_udp_missing_binary_never_launches_traffic_or_firewall_rules(udp_runtime):
    def fail(arguments):
        if arguments[0] == "iperf3":
            raise FileNotFoundError("iperf3")
    udp_runtime.fail = fail
    result = run_udp(udp_runtime)
    assert result["state"] == "failed" and result["receiver"]["status"] == "missing"
    assert not udp_runtime.launched and not udp_runtime.rules


def test_udp_unowned_dedicated_port_is_never_reused_or_killed(udp_runtime, monkeypatch):
    from room_demo import traffic_experiment as module
    execute = module.subprocess.run
    def command(arguments, **options):
        if "-lntup" in arguments:
            return SimpleNamespace(stdout="an unowned listener", stderr="", returncode=0)
        return execute(arguments, **options)
    monkeypatch.setattr(module.subprocess, "run", command)
    result = run_udp(udp_runtime)
    assert result["state"] == "failed" and "already occupied" in result["error"]
    assert not udp_runtime.launched and not udp_runtime.rules


def test_udp_cancelled_generation_has_explicit_endpoint_records_and_one_lazy_worker():
    running, release, cancelled = Event(), Event(), Event()
    calls = []
    def runner(container, target, phase, remaining_ms, *, admit, register, running):
        with admit():
            calls.append(container)
        running({})
        assert release.wait(2)
        with admit():
            pytest.fail("cancelled generation admitted")
    def publish(value):
        if value["state"] == "running":
            running.set()
        if value["state"] == "cancelled":
            cancelled.set()
    actor = TrafficExperiment("192.168.77.1", publish, udp_runner=runner)
    try:
        assert actor._thread is None
        actor.sync(("room", 1, 0), UDP_PHASE, "prpl-client-01", 5000)
        assert running.wait(2)
        worker = actor._thread
        actor.sync(("room", 1, 0), UDP_PHASE, "prpl-client-01", 4000)
        actor.sync(None)
        release.set()
        assert cancelled.wait(2)
        result = actor.snapshot()["history"][-1]
        assert result["state"] == "cancelled" and result["requested_offered_mbps"] == 8
        assert result["sender"]["status"] == result["receiver"]["status"] == "missing"
        assert len(calls) == 1 and actor._thread is worker
    finally:
        release.set()
        actor.close()


def test_ordinary_worlds_have_no_traffic_and_no_worker():
    assert validate_traffic({"roles": {}}) is None
    experiment = TrafficExperiment("192.168.77.1", Mock())
    experiment.sync(None)
    assert experiment.snapshot() == {"state": "idle", "history": []}
    assert experiment._thread is None
    experiment.close()


@pytest.mark.parametrize("change", [
    {"packets_per_second": True}, {"packets_per_second": 201}, {"payload_bytes": 1201},
    {"payload_bytes": 0}, {"end_ms": 28000}, {"role": "gateway"},
    {"target": "8.8.8.8"}, {"start_ms": -1}, {"end_ms": 5000},
])
def test_traffic_profile_rejects_unbounded_or_unbound_input(change):
    world = copy.deepcopy(WORLD)
    world["traffic_experiment"]["phases"] = [{**PHASE, **change}]
    with pytest.raises(ValueError):
        validate_traffic(world)


def test_phases_are_ordered_nonoverlapping_and_have_explicit_off_intervals():
    profile = validate_traffic(WORLD)
    assert phase_at(profile, 4999) is None
    assert phase_at(profile, 5000) == (0, PHASE)
    assert phase_at(profile, 10000) is None
    with pytest.raises(ValueError):
        validate_traffic({**WORLD, "traffic_experiment": {**profile, "phases": [PHASE, PHASE]}})


def test_generated_and_delivered_are_not_copied_from_requested_load():
    assert packet_counts("100 packets transmitted, 0 received, 100% packet loss") == {
        "transmitted_packets": 100, "received_echo_replies": 0}
    assert packet_counts("20 packets transmitted, 19 packets received")["received_echo_replies"] == 19
    assert packet_counts("ping: failed")["transmitted_packets"] is None


@pytest.mark.parametrize("target", ["8.8.8.8", "127.0.0.1", "224.0.0.1", "::1", "0.0.0.0",
                                   "169.254.1.1", "192.0.2.1", "255.255.255.255"])
def test_namespace_traffic_refuses_non_lab_destinations(target):
    with patch("room_demo.traffic_experiment.subprocess.run") as run:
        with pytest.raises(ValueError):
            namespace_ping("wlan-client", target, PHASE, 5000)
        run.assert_not_called()


def test_ping_binds_wireless_and_pins_network_namespace():
    with patch("room_demo.traffic_experiment.subprocess.run",
               return_value=SimpleNamespace(stdout='{"pid":42}')), \
         patch("room_demo.traffic_experiment.os.open", return_value=9) as opened, \
         patch("room_demo.traffic_experiment.os.close") as closed, \
         patch("room_demo.traffic_experiment.subprocess.Popen") as launch:
        _process, count = namespace_ping("wlan-client", "192.168.77.1", PHASE, 5000)
        command = launch.call_args.args[0]
        assert "--net=/proc/self/fd/9" in command
        assert command[command.index("-I") + 1] == "wlan0"
        assert command[-1] == "192.168.77.1" and 0 < count <= 50
        assert launch.call_args.kwargs["pass_fds"] == (9,)
        opened.assert_called_once()
        closed.assert_called_once_with(9)


def test_stale_admission_cannot_launch_after_pause_or_world_switch():
    experiment = TrafficExperiment("192.168.77.1", Mock())
    with pytest.raises(TrafficCancelled), experiment._admit(-1, float("inf")):
        pytest.fail("obsolete job admitted")
    experiment.close()


def test_phase_is_launched_once_and_cancellation_reaps_the_owned_process():
    running, finished = Event(), Event()
    process = Mock(pid=999999, returncode=None)
    process.poll.side_effect = lambda: process.returncode
    process.communicate.return_value = ("3 packets transmitted, 2 received", None)
    launches = []
    def launch(container, target, phase, remaining_ms, *, admit):
        with admit():
            launches.append((container, phase, remaining_ms))
            return process, 50
    def publish(value):
        if value["state"] == "running":
            running.set()
        if value["history"]:
            finished.set()
    def signal_process(_process, _signal):
        process.returncode = 130
    experiment = TrafficExperiment("192.168.77.1", publish, launcher=launch)
    experiment._signal = signal_process
    try:
        experiment.sync(("room", 1, 0), PHASE, "wlan-client", 5000)
        assert running.wait(2)
        experiment.sync(("room", 1, 0), PHASE, "wlan-client", 4000)
        experiment.sync(None)
        assert finished.wait(2)
        result = experiment.snapshot()["history"][-1]
        assert result["state"] == "cancelled"
        assert result["transmitted_packets"] == 3
        assert result["received_echo_replies"] == 2
        assert len(launches) == 1
    finally:
        experiment.close()
    process.communicate.assert_called_once()

@pytest.mark.parametrize("change", [
    {"_closing": True}, {"_faulted": "medium changed"}, {"_lease": None},
    {"_playback_status": "paused"}, {"_roles": {"client": {"present": False}}},
])
def test_session_traffic_gate_cancels_for_pause_expiry_fault_or_offline(change):
    from room_demo.interactions import InteractiveMediumSession
    actor = Mock()
    session = SimpleNamespace(_traffic_experiment=actor, _playback_world={**WORLD, "golden_sha256": "world"},
                              _playback_time_ms=6000, _closing=False, _faulted=None, _lease={},
                              _playback_status="playing", _roles={"client": {"present": True}},
                              _traffic_run=1, plan={"bindings": {"client": {"container": "wlan-client"}}})
    InteractiveMediumSession._sync_traffic(session)
    actor.sync.assert_called_once_with(("world", 1, 0), PHASE, "wlan-client", 4000,
                                       complete_through=("world", 1, 6000))
    actor.reset_mock()
    for field, value in change.items():
        setattr(session, field, value)
    InteractiveMediumSession._sync_traffic(session)
    actor.sync.assert_called_once_with(None)


def test_cancel_during_namespace_resolution_revokes_launch_admission():
    resolving, release, cancelled = Event(), Event(), Event()
    launched = []
    def launch(container, target, phase, remaining_ms, *, admit):
        resolving.set()
        assert release.wait(2)
        with admit():
            launched.append(True)
            raise AssertionError("cancelled phase must not start a process")
    def publish(value):
        if value["state"] == "cancelled":
            cancelled.set()
    actor = TrafficExperiment("192.168.77.1", publish, launcher=launch)
    try:
        actor.sync(("room", 1, 0), PHASE, "wlan-client", 5000)
        assert resolving.wait(2)
        actor.sync(None)
        release.set()
        assert cancelled.wait(2)
        assert not launched
    finally:
        release.set()
        actor.close()

def test_traffic_cleanup_failure_cannot_skip_rf_restoration():
    from room_demo.interactions import InteractiveMediumSession
    actor = Mock()
    actor.close.side_effect = RuntimeError("traffic worker failed to stop")
    client = Mock()
    session = SimpleNamespace(_lock=RLock(), _closing=False, _pause_playback=Mock(), _movements={},
                              _recording=None, _movement_threads={}, _playback_thread=None, _restored=False,
                              _traffic_experiment=actor, _client=client, restore=Mock(return_value=True))
    session.stop_traffic = lambda: InteractiveMediumSession.stop_traffic(session)
    with pytest.raises(RuntimeError, match="failed to stop"):
        InteractiveMediumSession.close(session)
    session.restore.assert_called_once()
    client.close.assert_called_once()
    assert session._client is None


def test_udp_shutdown_does_not_hide_cleanup_failure_as_cancellation():
    actor = TrafficExperiment("192.168.77.1", Mock(), udp_runner=Mock(return_value={
        "state": "failed", "cleanup_errors": ["owned firewall rule remains"]}))
    actor._closed = True
    actor._udp_job({"key": ("room", 1, 0), "phase": UDP_PHASE, "container": "prpl-client-01",
                    "deadline": time.monotonic() + 10}, 0)
    assert actor.snapshot()["state"] == "failed"
    assert actor.snapshot()["cleanup_errors"] == ["owned firewall rule remains"]
    assert actor._udp_cleanup_failed is True


@pytest.mark.parametrize("operation", ["lease_expiry", "shutdown"])
def test_real_session_lifecycle_revokes_and_joins_udp_worker(tmp_path, operation):
    from test_interactions import FakeClient, LAYOUT, PLAN, WORLD as SESSION_WORLD
    from room_demo.events import EventStore
    from room_demo.interactions import InteractiveMediumSession
    started, cancelled = Event(), Event()
    processes = [Mock(pid=999998, returncode=None), Mock(pid=999999, returncode=None)]
    for process in processes:
        process.poll.side_effect = lambda child=process: child.returncode
    def stop(process, _signum):
        process.returncode = 130
    def runner(container, target, phase, remaining_ms, *, admit, register, running):
        try:
            with admit():
                for process in processes:
                    register(process)
            running({})
            started.set()
            while not cancelled.wait(0.01):
                with admit():
                    pass
        finally:
            for process in processes:
                stop(process, signal.SIGINT)
            cancelled.set()
    actor = TrafficExperiment("192.168.77.1", Mock(), udp_runner=runner)
    actor._signal = stop
    world = {**copy.deepcopy(SESSION_WORLD), "golden_sha256": "test-world",
             "traffic_experiment": {"schema": "easymesh.room-traffic.v1",
                 "phases": [{**UDP_PHASE, "role": "sta_01", "start_ms": 0, "end_ms": 10000}]}}
    store = EventStore("traffic-lifecycle", world, tmp_path / "events.jsonl", persist=False)
    client = FakeClient("unused")
    session = InteractiveMediumSession(store, world, LAYOUT, PLAN, "unused", traffic_experiment=actor,
                                       client_factory=lambda _path: client)
    try:
        session.start()
        lease = session.acquire("unit-test")
        session.playback_control("play", token=lease["token"], expected_revision=session.snapshot()["revision"])
        assert started.wait(2)
        if operation == "lease_expiry":
            session._lease["expires_monotonic"] = time.monotonic() - 1
            snapshot = session.snapshot()
            assert snapshot["lease"]["held"] is False and snapshot["playback"]["status"] == "paused"
        else:
            assert session.close() is True
            assert not actor._thread.is_alive() and client.closed
        assert cancelled.wait(2)
        assert all(process.returncode == 130 for process in processes)
    finally:
        session.close()
    assert actor.snapshot()["history"][-1]["state"] == "cancelled"


def traffic_session(actor):
    from room_demo.interactions import InteractiveMediumSession
    session = SimpleNamespace(_traffic_experiment=actor,
                              _playback_world={**copy.deepcopy(WORLD), "golden_sha256": "world"},
                              _playback_time_ms=6000, _closing=False, _faulted=None, _lease={},
                              _playback_status="playing", _roles={"client": {"present": True}},
                              _traffic_run=1, plan={"bindings": {"client": {"container": "wlan-client"}}})
    session.sync = lambda: InteractiveMediumSession._sync_traffic(session)
    return session


@pytest.fixture
def ping_runtime(monkeypatch):
    from room_demo import traffic_experiment as module
    state = SimpleNamespace(clock=100.0, processes=[], signals=[], launches=[], expected_results=1,
                            finished=Event(), transition=None, cleanup=None,
                            output="535 packets transmitted, 534 received")
    monkeypatch.setattr(module, "time", SimpleNamespace(**vars(module.time)))
    monkeypatch.setattr(module.time, "monotonic", lambda: state.clock)

    def launch(container, target, phase, remaining_ms, *, admit):
        with admit():
            process = Mock(pid=999999, returncode=None)
            process.poll.side_effect = lambda: process.returncode
            def communicate(**_options):
                if state.cleanup:
                    state.cleanup()
                return state.output, None
            process.communicate.side_effect = communicate
            state.processes.append(process)
            state.launches.append((container, remaining_ms, state.clock))
            return process, 536

    def publish(value):
        if value["state"] == "running":
            state.transition(value)
        if len(value["history"]) == state.expected_results:
            state.finished.set()

    def stop(process, signum):
        state.signals.append((process, signum, state.clock))
        process.returncode = 130

    state.actor = TrafficExperiment("192.168.77.1", publish, launcher=launch)
    state.actor._signal = stop
    state.session = traffic_session(state.actor)
    yield state
    state.actor.close()
    assert all(process.returncode is not None for process in state.processes)


@pytest.mark.parametrize("ending", ["deadline", "phase-end", "playback-end"])
def test_ping_natural_completion_keeps_actual_counts_and_stops_at_boundary(ping_runtime, ending):
    state = ping_runtime
    def transition(_value):
        state.clock = 104.0 if ending == "deadline" else 103.9
        if ending != "deadline":
            state.session._playback_time_ms = 10000 if ending == "phase-end" else 30000
            if ending == "playback-end":
                state.session._playback_status = "completed"
            state.session.sync()
    state.transition = transition
    state.session.sync()
    assert state.finished.wait(2)
    result = state.actor.snapshot()["history"][-1]
    assert result["state"] == "completed"
    assert result["requested_packets"] == 536 and result["transmitted_packets"] == 535
    assert result["received_echo_replies"] == 534
    assert len(state.launches) == 1 and state.launches[0][1] == 4000
    assert state.signals and all(when <= 104 for _process, _signum, when in state.signals)
    assert state.processes[0].communicate.call_count == 1
    with pytest.raises(TrafficCancelled), state.actor._admit(1, 104):
        pytest.fail("ended phase admitted more traffic")


def test_adjacent_phase_completes_old_job_and_launches_new_job_once(ping_runtime):
    state = ping_runtime
    state.expected_results = 2
    following = {**PHASE, "start_ms": 10000, "end_ms": 15000}
    state.session._playback_world["traffic_experiment"]["phases"].append(following)
    def transition(value):
        if value["key"][-1] == 0:
            state.clock = 103.9
            state.session._playback_time_ms = 10000
            state.session.sync()
            state.session.sync()
        else:
            state.clock = 108.9
            state.session._playback_time_ms = 15000
            state.session.sync()
    state.transition = transition
    state.session.sync()
    assert state.finished.wait(2)
    history = state.actor.snapshot()["history"]
    assert [result["key"][-1] for result in history] == [0, 1]
    assert [result["state"] for result in history] == ["completed", "completed"]
    assert [launch[1] for launch in state.launches] == [4000, 5000]
    assert all(process.communicate.call_count == 1 for process in state.processes)


@pytest.mark.parametrize("interruption", ["pause", "fault", "lease", "offline", "world", "run", "close"])
def test_early_cancellation_is_not_relabelled_by_late_cleanup(ping_runtime, interruption):
    state = ping_runtime
    def transition(_value):
        state.clock = 101
        if interruption == "pause":
            state.session._playback_status = "paused"
        elif interruption == "fault":
            state.session._faulted = "medium changed"
        elif interruption == "lease":
            state.session._lease = None
        elif interruption == "offline":
            state.session._roles["client"]["present"] = False
        elif interruption == "world":
            state.session._playback_world["golden_sha256"] = "other-world"
        elif interruption == "run":
            state.session._traffic_run += 1
        else:
            state.session._closing = True
        state.session.sync()
        state.actor.sync(None)
    def cleanup():
        state.clock = 110
        state.actor.sync(None, complete_through=("world", 1, 10000))
    state.transition, state.cleanup = transition, cleanup
    state.session.sync()
    assert state.finished.wait(2)
    result = state.actor.snapshot()["history"][-1]
    assert result["state"] == "cancelled" and result["elapsed_seconds"] == 10
    assert result["transmitted_packets"] == 535 and result["received_echo_replies"] == 534
    assert len(state.launches) == 1
    assert state.signals and state.signals[0][2] == 101


@pytest.mark.parametrize("ended", ["deadline", "phase-end", "process-exit"])
@pytest.mark.parametrize("cleanup_action", ["pause", "world"])
def test_completed_ping_is_not_retroactively_cancelled_during_cleanup(ping_runtime, ended, cleanup_action):
    state = ping_runtime
    def transition(_value):
        if ended == "process-exit":
            state.clock = 103
            state.processes[0].returncode = 0
        elif ended == "deadline":
            state.clock = 104
        else:
            state.clock = 103.9
            state.session._playback_time_ms = 10000
            state.session.sync()
    def cleanup():
        if cleanup_action == "world":
            state.session._playback_world["golden_sha256"] = "other-world"
        else:
            state.session._playback_status = "paused"
        state.session.sync()
        state.actor.sync(None)
        state.clock = 110
    state.transition, state.cleanup = transition, cleanup
    state.session.sync()
    assert state.finished.wait(2)
    assert state.actor.snapshot()["history"][-1]["state"] == "completed"
    assert len(state.launches) == 1


@pytest.mark.parametrize("context", [("other-world", 1, 10000), ("world", 2, 10000), ("world", 1, 9999)])
def test_phase_completion_requires_matching_world_run_and_elapsed_phase(ping_runtime, context):
    state = ping_runtime
    def transition(_value):
        state.clock = 101
        state.actor.sync(None, complete_through=context)
    state.transition = transition
    state.session.sync()
    assert state.finished.wait(2)
    assert state.actor.snapshot()["history"][-1]["state"] == "cancelled"


def test_natural_deadline_without_ping_summary_does_not_fabricate_counts(ping_runtime):
    state = ping_runtime
    state.output = "ping failed before producing counters"
    state.transition = lambda _value: setattr(state, "clock", 104)
    state.session.sync()
    assert state.finished.wait(2)
    result = state.actor.snapshot()["history"][-1]
    assert result["state"] == "failed"
    assert result["transmitted_packets"] is result["received_echo_replies"] is None


@pytest.mark.parametrize("cancel_early", [False, True])
def test_deadline_during_setup_never_launches_or_hides_prior_cancellation(ping_runtime, cancel_early):
    state = ping_runtime
    def launch(container, target, phase, remaining_ms, *, admit):
        if cancel_early:
            state.clock = 101
            state.actor.sync(None)
        state.clock = 110
        with admit():
            pytest.fail("setup launched traffic after its deadline")
    state.actor.launcher = launch
    state.session.sync()
    assert state.finished.wait(2)
    result = state.actor.snapshot()["history"][-1]
    assert result["state"] == ("cancelled" if cancel_early else "expired")
    assert result["transmitted_packets"] is result["received_echo_replies"] is None
    assert not state.processes


@pytest.mark.parametrize("ending", ["phase-end", "deadline", "pause", "world", "late-pause"])
@pytest.mark.parametrize("outcome", ["complete", "partial", "failed", "cleanup-failed"])
def test_udp_phase_end_preserves_measured_endpoint_and_failure_status(monkeypatch, ending, outcome):
    from room_demo import traffic_experiment as module
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(module, "time", SimpleNamespace(**vars(module.time)))
    monkeypatch.setattr(module.time, "monotonic", lambda: clock.now)
    result = {"state": "completed" if outcome == "complete" else "cancelled" if outcome == "partial" else "failed"}
    for endpoint, sender in (("sender", True), ("receiver", False)):
        result[endpoint] = {**udp_endpoint_record(json.dumps(udp_output(sender)), sender), "returncode": 0}
    if outcome == "partial":
        result["receiver"].update(status="partial", returncode=130)
    if outcome == "cleanup-failed":
        result["cleanup_errors"] = ["owned firewall rule remains"]
    expected = copy.deepcopy(result)
    actor = TrafficExperiment("192.168.77.1", Mock())
    job = {"key": ("world", 1, 0), "phase": UDP_PHASE, "container": "wlan-client", "deadline": 105}
    actor._desired = job
    def runner(*_args, **_kwargs):
        clock.now = 104
        if ending in {"phase-end", "late-pause"}:
            actor.sync(None, complete_through=("world", 1, 10000))
        elif ending == "pause":
            actor.sync(None)
        elif ending == "world":
            actor.sync(None, complete_through=("other-world", 1, 10000))
        clock.now = 110
        if ending == "late-pause":
            actor.sync(None)
        return result
    actor.udp_runner = runner
    try:
        actor._udp_job(job, 0)
        actual = actor.snapshot()["history"][-1]
        if outcome in {"failed", "cleanup-failed"}:
            assert actual["state"] == "failed"
        elif ending in {"pause", "world"}:
            assert actual["state"] == "cancelled"
        else:
            assert actual["state"] == ("completed" if outcome == "complete" else "failed")
        assert actual["sender"] == expected["sender"]
        assert actual["receiver"] == expected["receiver"]
        assert actual.get("cleanup_errors") == expected.get("cleanup_errors")
    finally:
        actor.close()
