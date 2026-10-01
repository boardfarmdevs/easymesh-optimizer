import importlib.util
from contextlib import nullcontext
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


SPEC = importlib.util.spec_from_file_location(
    "retry_acceptance", Path(__file__).with_name("native-retry-counter-acceptance.py"))
DRIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRIVER)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def unexpected(*arguments, **keywords):
        pytest.fail("unexpected runtime operation")

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "medium/configurator"))
    monkeypatch.setattr(DRIVER.subprocess, "run", unexpected)
    monkeypatch.setattr(DRIVER.subprocess, "Popen", unexpected)
    monkeypatch.setattr(DRIVER.urllib.request, "urlopen", unexpected)
    monkeypatch.setattr(DRIVER.time, "sleep", unexpected)


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=0)

    def advance(seconds):
        clock.now += seconds

    monkeypatch.setattr(DRIVER.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(DRIVER.time, "sleep", advance)
    return clock


@pytest.mark.parametrize('settings', [
    ('eth0', 1, 200, 1400), ('wifi1.1', 1, 200, 1400), ('wifi0', 0, 200, 1400),
    ('wifi0', 121, 200, 1400), ('wifi0', 1, 0, 1400), ('wifi0', 1, 1001, 1400),
    ('wifi0', 1, 200, 127), ('wifi0', 1, 200, 1401), ('wifi0', 1, True, 1400)])
def test_broadcast_rejects_invalid_bounds_before_opening_socket(monkeypatch, settings):
    def unexpected(*arguments):
        pytest.fail('invalid broadcast opened a socket')
    monkeypatch.setattr(DRIVER.socket, 'socket', unexpected)
    with pytest.raises(RuntimeError):
        DRIVER.broadcast(*settings)


@pytest.mark.parametrize('payload', [127, 1401, True, 128.5])
def test_udp_endpoint_rejects_invalid_payload_before_socket(monkeypatch, payload):
    monkeypatch.setattr(DRIVER.socket, 'socket', lambda *arguments: pytest.fail('invalid payload opened socket'))
    with pytest.raises(RuntimeError, match='UDP payload'):
        DRIVER.endpoint('send', '192.0.2.1', 1, 55209, payload)


@pytest.mark.parametrize('payload', [128, 1000, 1400])
def test_udp_endpoint_sends_configured_real_payload(clock, monkeypatch, capsys, payload):
    channel = Mock()
    monkeypatch.setattr(DRIVER.socket, 'socket', lambda *arguments: nullcontext(channel))
    DRIVER.endpoint('send', '192.0.2.1', 1, 55209, payload)
    report = json.loads(capsys.readouterr().out)
    assert report['packets'] == channel.sendto.call_count == 400
    assert report['payload_bytes'] == payload
    for index, call in enumerate(channel.sendto.call_args_list):
        data, destination = call.args
        assert destination == ('192.0.2.1', 55209)
        assert data == index.to_bytes(4, 'big') + bytes(payload - 4)


def test_udp_receiver_counts_only_selected_payload_and_unique_sequences(clock, monkeypatch, capsys):
    channel = Mock()
    frames = [bytes(128), bytes(128), bytes(1000), bytes(127) + b'x', (1).to_bytes(4, 'big') + bytes(124)]
    def receive(size):
        clock.now += .4
        return frames.pop(0)
    channel.recv.side_effect = receive
    monkeypatch.setattr(DRIVER.socket, 'socket', lambda *arguments: nullcontext(channel))
    DRIVER.endpoint('receive', '192.0.2.1', 1, 55209, 128)
    report = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert report['packets'] == 3 and report['unique_packets'] == 2
    assert report['payload_bytes'] == 128


def test_udp_progress_reports_real_counts_before_completion(clock, monkeypatch, capsys):
    channel = Mock()
    monkeypatch.setattr(DRIVER.socket, 'socket', lambda *arguments: nullcontext(channel))
    DRIVER.endpoint('send', '192.0.2.1', 2, 55209, 128, progress=True)
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert reports[0]['kind'] == 'progress' and reports[0]['mode'] == 'send'
    assert reports[0]['packets'] == 400 and reports[0]['seconds'] == 1
    assert reports[-1]['packets'] == 800 and 'kind' not in reports[-1]


@pytest.mark.parametrize('category', ['best-effort', 'voice'])
def test_udp_marks_voice_packets_without_changing_socket_for_best_effort(clock, monkeypatch, capsys, category):
    channel = Mock()
    monkeypatch.setattr(DRIVER.socket, 'socket', lambda *arguments: nullcontext(channel))
    DRIVER.endpoint('send', '192.0.2.1', 1, 55209, 128, access_category=category)
    if category == 'voice':
        channel.setsockopt.assert_called_once_with(DRIVER.socket.IPPROTO_IP, DRIVER.socket.IP_TOS, 0xc0)
    else:
        channel.setsockopt.assert_not_called()
    assert json.loads(capsys.readouterr().out)['requested_access_category'] == category


@pytest.mark.parametrize('drop_first', [False, True])
def test_broadcast_uses_real_bounded_ethernet_frames_without_catchup(clock, monkeypatch, capsys, drop_first):
    channel = Mock()
    channel.getsockname.return_value = ('wifi0', 0, 0, 0, bytes.fromhex('020000000001'))
    frames = []
    attempted_at = []
    def send(frame):
        attempted_at.append(clock.now)
        if drop_first and len(attempted_at) == 1:
            clock.now += .4
            raise TimeoutError()
        frames.append(frame)
        return len(frame)
    channel.send.side_effect = send
    monkeypatch.setattr(DRIVER.socket, 'socket', lambda *arguments: nullcontext(channel))
    DRIVER.broadcast('wifi0', 1, 4, 128)
    channel.bind.assert_called_once_with(('wifi0', 0))
    output = capsys.readouterr().out.splitlines()
    assert output[0] == 'ready'
    report = json.loads(output[1])
    assert report['packets'] == len(frames) > 0
    assert report['attempted'] == len(attempted_at) == 4
    assert report['source'] == 'bound_ap_non_ip_broadcast'
    for index, frame in enumerate(frames):
        assert frame[:14] == bytes.fromhex('ffffffffffff02000000000188b5')
        assert frame[14:18] == index.to_bytes(4, 'big')
        assert frame[18:] == bytes(124)
    assert all(after - before >= .25 for before, after in zip(attempted_at, attempted_at[1:]))


def test_broadcast_rejects_partial_frame(clock, monkeypatch):
    channel = Mock()
    channel.getsockname.return_value = ('wifi0', 0, 0, 0, bytes(6))
    channel.send.return_value = 1
    monkeypatch.setattr(DRIVER.socket, 'socket', lambda *arguments: nullcontext(channel))
    with pytest.raises(RuntimeError, match='incomplete background Ethernet frame'):
        DRIVER.broadcast('wifi0', 1, 4, 128)


def test_broadcast_upper_bound_is_paced_without_bursts(clock, monkeypatch, capsys):
    channel = Mock()
    channel.getsockname.return_value = ('wifi0', 0, 0, 0, bytes(6))
    sent_at = []
    def send(frame):
        sent_at.append(clock.now)
        return len(frame)
    channel.send.side_effect = send
    monkeypatch.setattr(DRIVER.socket, 'socket', lambda *arguments: nullcontext(channel))
    DRIVER.broadcast('wifi0', 1, 1000, 128)
    report = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert report['offered_packets_per_second'] == 1000
    assert 999 <= report['packets'] <= 1001
    assert all(after - before >= .000999 for before, after in zip(sent_at, sent_at[1:]))


def trial(name="ack_loss", unit=1):
    driver = {"packets_sent": 1000, "bytes_sent": 1024000,
              "retransmissions": 900, "tx_packet_errors": 45, "rx_packet_errors": 0}
    return {"name": name, "kernel_deltas": driver,
            "native_deltas": {**driver, "bytes_sent": driver["bytes_sent"] // unit},
            "sender": {"packets": 1000}, "receiver": {"unique_packets": 1000}}


@pytest.mark.parametrize("unit", [1, 1024])
def test_ack_loss_allows_delivered_data_despite_tx_failure(unit):
    DRIVER.qualify_trial(trial(unit=unit), unit)


def test_missing_native_tx_errors_fails_even_when_retries_are_reported():
    value = trial()
    value["native_deltas"]["tx_packet_errors"] = 0
    with pytest.raises(RuntimeError, match="native tx_packet_errors"):
        DRIVER.qualify_trial(value, 1)


def test_wrong_byte_units_cannot_qualify():
    with pytest.raises(RuntimeError, match="byte units"):
        DRIVER.qualify_trial(trial(unit=1024), 1)


def test_data_loss_requires_actual_endpoint_loss():
    with pytest.raises(RuntimeError, match="did not lose data"):
        DRIVER.qualify_trial(trial("data_loss"), 1)
    value = trial("data_loss")
    value["receiver"]["unique_packets"] = 950
    DRIVER.qualify_trial(value, 1)


def test_counter_reset_cannot_qualify():
    value = trial()
    value["native_deltas"]["rx_packet_errors"] = -1
    with pytest.raises(RuntimeError, match="counter reset"):
        DRIVER.qualify_trial(value, 1)


def test_kernel_counter_parser_keeps_missing_distinct_from_zero():
    values = DRIVER.kernel_counters("  tx packets: 100\n  tx retries: 9\n  tx failed: 2\n  rx drop misc: 0\n")
    assert values == {"packets_sent": 100, "retransmissions": 9, "tx_packet_errors": 2, "rx_packet_errors": 0}
    assert "bytes_sent" not in values


def health(active=20, expected=20, healthy=True):
    return {"healthy": healthy, "api_active": active, "expected_online_clients": expected}


def cleanup_report(check):
    return {"state": "passed", "counters_passed": True, "room_health": check}


@pytest.mark.parametrize("state,active", [("active", True), ("inactive", False), ("failed", False)])
def test_service_activity_is_sampled_without_starting_it(monkeypatch, state, active):
    command = Mock(return_value=state + "\n")
    monkeypatch.setattr(DRIVER, "command", command)
    assert DRIVER.room_service_active("room") is active
    command.assert_called_once_with("systemctl", "show", "--property=ActiveState", "--value", "room")


def test_transitional_service_state_is_not_treated_as_inactive(monkeypatch):
    monkeypatch.setattr(DRIVER, "command", Mock(return_value="activating\n"))
    with pytest.raises(RuntimeError, match="not stable"):
        DRIVER.room_service_active("room")


def test_health_uses_read_only_current_endpoint(monkeypatch):
    sample = {**health(), "evidence_storage": {"large": "unrelated"}}
    request = Mock(return_value=io.StringIO(json.dumps({"health": sample})))
    monkeypatch.setattr(DRIVER.urllib.request, "urlopen", request)
    assert DRIVER.room_health_sample(3) == health()
    request.assert_called_once_with("http://127.0.0.1:8891/api/demo/current", timeout=3)


def test_inactive_room_health_is_explicitly_not_checked():
    check = DRIVER.initial_room_health(False, 20)
    DRIVER.check_room_health(check)
    assert check["state"] == "not_checked"
    assert check["initial"] is check["final"] is None
    assert check["reason"] == "room service was not active"


@pytest.mark.parametrize("sample", [{}, {"healthy": True, "api_active": 20},
                                    health(expected=18), health(expected="20"),
                                    health(healthy=None), health(active=None)])
def test_missing_initial_expected_health_skips_cleanup_polling(monkeypatch, sample):
    sampler = Mock(return_value=sample)
    monkeypatch.setattr(DRIVER, "room_health_sample", sampler)
    check = DRIVER.initial_room_health(True, 20)
    DRIVER.check_room_health(check)
    assert check["state"] == "not_checked"
    assert check["initial"] == sample and check["final"] is None
    assert "initial room health not sampled" in check["reason"]
    sampler.assert_called_once_with(5)


def test_initial_health_request_error_is_reported_without_polling(monkeypatch):
    sampler = Mock(side_effect=TimeoutError("initial sample timed out"))
    monkeypatch.setattr(DRIVER, "room_health_sample", sampler)
    check = DRIVER.initial_room_health(True, 20)
    DRIVER.check_room_health(check)
    assert check["state"] == "not_checked"
    assert "TimeoutError: initial sample timed out" in check["reason"]
    sampler.assert_called_once_with(5)


@pytest.mark.parametrize("sample", [health(active=18, healthy=False), health(active=18),
                                    health(expected=18, active=18), health(healthy=False), {}])
def test_degraded_room_cannot_pass_or_lower_expected_count(monkeypatch, clock, sample):
    sampler = Mock(return_value=health(active=18, healthy=False))
    monkeypatch.setattr(DRIVER, "room_health_sample", sampler)
    check = DRIVER.initial_room_health(True, 20)
    assert check["state"] == "pending"
    sampler.return_value = sample
    sampler.reset_mock()
    DRIVER.check_room_health(check)
    assert check["state"] == "failed"
    assert check["expected_online_clients"] == 20
    assert check["initial"]["api_active"] == 18
    assert check["final"] == sample
    assert "20 clients" in check["reason"]
    assert clock.now == check["timeout_seconds"] == 60
    assert 0 < sampler.call_count <= 60
    assert all(0 < call.args[0] <= 5 for call in sampler.call_args_list)
    assert sampler.call_args_list[-1].args[0] <= 1


def test_room_recovery_tolerates_startup_errors_and_retains_initial_sample(monkeypatch, clock):
    sampler = Mock(side_effect=[health(), TimeoutError("starting"), health(active=18, healthy=False), health()])
    monkeypatch.setattr(DRIVER, "room_health_sample", sampler)
    check = DRIVER.initial_room_health(True, 20)
    DRIVER.check_room_health(check)
    assert check["state"] == "passed"
    assert check["initial"] == check["final"] == health()
    assert "last_error" not in check
    assert clock.now == 2


def test_health_request_timeouts_remain_bounded_and_visible(monkeypatch, clock):
    monkeypatch.setattr(DRIVER, "room_health_sample", Mock(return_value=health()))
    check = DRIVER.initial_room_health(True, 20)
    timeouts = []

    def unavailable(timeout):
        timeouts.append(timeout)
        clock.now += timeout
        raise TimeoutError("room unavailable")

    monkeypatch.setattr(DRIVER, "room_health_sample", unavailable)
    DRIVER.check_room_health(check)
    assert check["state"] == "failed" and check["final"] is None
    assert check["last_error"] == "TimeoutError: room unavailable"
    assert clock.now == 60 and len(timeouts) <= 12


def test_healthy_response_after_deadline_cannot_pass(monkeypatch, clock):
    monkeypatch.setattr(DRIVER, "room_health_sample", Mock(return_value=health()))
    check = DRIVER.initial_room_health(True, 20)

    def late(timeout):
        clock.now += 61
        return health()

    monkeypatch.setattr(DRIVER, "room_health_sample", late)
    DRIVER.check_room_health(check)
    assert check["state"] == "failed"


def test_native_counters_and_fixture_restoration_do_not_claim_room_health(monkeypatch, clock):
    monkeypatch.setattr(DRIVER, "room_health_sample", Mock(return_value=health(active=18, healthy=False)))
    report = cleanup_report(DRIVER.initial_room_health(True, 20))
    original = ("02:00:00:00:01:01", 2412)
    DRIVER.finish_cleanup(report, [], lambda: original, original)
    assert report["counters_passed"] and report["restored"]
    assert report["association_after_cleanup"] == original
    assert "tested_sta_association" in report["restored_scope"]
    assert report["room_health"]["state"] == report["state"] == "failed"


def test_cleanup_continues_after_errors_and_saves_association_read_failure(monkeypatch, clock):
    monkeypatch.setattr(DRIVER, "room_health_sample", Mock(return_value=health()))
    report = cleanup_report(DRIVER.initial_room_health(True, 20))
    stop = Mock(side_effect=TimeoutError("endpoint still running"))
    restore_rf = Mock(side_effect=RuntimeError("RF readback mismatch"))
    restart = Mock()
    identity = Mock(side_effect=RuntimeError("association read failed"))
    DRIVER.finish_cleanup(report, [("stop UDP endpoint", stop), ("restore RF", restore_rf),
                                  ("restart room", restart)], identity, ("original", 2412))
    restart.assert_called_once_with()
    assert len(report["cleanup_errors"]) == 3
    assert "RF readback mismatch" in report["cleanup_errors"][1]
    assert "association read failed" in report["cleanup_errors"][2]
    assert report["counters_passed"] and not report["restored"]
    assert report["room_health"]["state"] == "passed" and report["state"] == "failed"


def test_association_mismatch_is_reported_without_reassociation():
    report = cleanup_report(DRIVER.initial_room_health(False, 20))
    DRIVER.finish_cleanup(report, [], lambda: ("different", 5180), ("original", 2412))
    assert not report["restored"] and report["state"] == "failed"
    assert report["association_after_cleanup"] == ("different", 5180)
    assert "tested STA association differs" in report["cleanup_errors"][0]


def test_association_is_verified_before_resuming_room_optimizer(monkeypatch):
    monkeypatch.setattr(DRIVER, "room_health_sample", Mock(return_value=health()))
    report = cleanup_report(DRIVER.initial_room_health(True, 20))
    original = ("original", 2412)
    association = {"current": original}
    order = []

    def identity():
        order.append("verify association")
        return association["current"]

    def resume():
        order.append("resume optimizer")
        association["current"] = ("native room target", 5180)

    DRIVER.finish_cleanup(report, [], identity, original, [("resume room", resume)])
    assert order == ["verify association", "resume optimizer"]
    assert report["association_after_cleanup"] == original
    assert report["restored"] and report["state"] == "passed"
    assert report["room_health"]["state"] == "passed"


@pytest.mark.parametrize("state", ["passed", "failed"])
def test_successful_cleanup_preserves_trial_result_and_unchecked_health(state):
    report = cleanup_report(DRIVER.initial_room_health(False, 20))
    report["state"] = state
    DRIVER.finish_cleanup(report, [], lambda: ("original", 2412), ("original", 2412))
    assert report["state"] == state and report["restored"]
    assert report["room_health"]["state"] == "not_checked"


@pytest.mark.parametrize("stack", ["prpl", "rdk"])
@pytest.mark.parametrize("was_active", [True, False])
def test_main_preserves_service_state_and_writes_report_after_cleanup_error(
        monkeypatch, tmp_path, stack, was_active):
    from wmdcfg import actuator, rf_qualify, rf_spatial

    service = "easymesh-room-service" if stack == "rdk" else "prplmesh-room-service"
    controller = "bpibroadband" if stack == "rdk" else "prpl-controller"
    original = ("02:00:00:00:01:01", 2412)
    activity = {"active": was_active}
    commands = []

    def command(*arguments, **keywords):
        commands.append(arguments)
        if arguments[:2] == ("systemctl", "show"):
            return "active" if activity["active"] else "inactive"
        if arguments[0] == "systemctl" and arguments[1] in ("start", "stop"):
            activity["active"] = arguments[1] == "start"
            return ""
        if arguments[:2] == ("lxc", "query"):
            return json.dumps({"pid": 123})
        if arguments[:2] == ("lxc", "exec"):
            if arguments[-1] == "link":
                return "Connected to 02:00:00:00:01:01 (on wlan0)\n  freq: 2412\n"
            if arguments[-1] == "dev":
                return arguments[2]
            if arguments[-1] == "/sys/class/net/wlan0/address":
                return "02:00:00:00:02:01"
        if arguments[:4] == ("nsenter", "-t", "123", "-n"):
            if arguments[4:8] == ("ip", "-j", "-4", "addr"):
                return json.dumps([{"addr_info": [{"local": "192.0.2.1", "scope": "global"}]}])
            if arguments[4:7] == ("ip", "-j", "link"):
                return json.dumps([{"master": "br0"}])
            if arguments[4:6] == ("ip", "route"):
                return ""
            if arguments[4] == "ping":
                raise RuntimeError("trial unavailable")
        pytest.fail(f"unexpected command: {arguments}")

    interaction = {"lease": {"held": False}, "recording": {"active": False},
                   "selected_world": "home-five-agent--private-client-room-walk", "expected_online_clients": 20,
                   "playback": {"status": "paused", "time_ms": 0, "manual_roles": []}}
    requests = []

    def request(url, timeout):
        requests.append(url)
        if url.endswith("/interactions"):
            return io.StringIO(json.dumps(interaction))
        assert url.endswith("/current") and activity["active"]
        return io.StringIO(json.dumps({"health": health()}))

    control = SimpleNamespace(status=lambda: SimpleNamespace(instance_id="medium", generation=1),
                              get_frequency_link=lambda *arguments: (1, 40, True),
                              apply_frequency=Mock())
    provider = SimpleNamespace(close=Mock(side_effect=RuntimeError("provider close failed")))
    monkeypatch.setitem(DRIVER.sys.modules, "optimizer.load_observer",
                        SimpleNamespace(NativeLoadProvider=Mock(return_value=provider)))
    monkeypatch.setattr(actuator, "ControlClient", lambda *arguments: nullcontext(control))
    monkeypatch.setattr(rf_qualify, "MediumRestarter", lambda stack: SimpleNamespace(socket="socket", links=[]))
    monkeypatch.setattr(rf_spatial, "registered_radio", lambda node, interface, registered: node)
    monkeypatch.setattr(rf_spatial, "private_channels", lambda node: {
        "ap0": {"bssid": original[0], "frequency": original[1]}} if node == controller else {})
    monkeypatch.setattr(DRIVER, "command", command)
    monkeypatch.setattr(DRIVER.urllib.request, "urlopen", request)
    monkeypatch.setattr(DRIVER.os, "geteuid", lambda: 0)
    output = tmp_path / "evidence"
    monkeypatch.setattr(DRIVER.sys, "argv", ["retry", "--stack", stack, "--yes-change-lab", "--output", str(output)])
    assert DRIVER.main() == 1
    report = json.loads((output / "report.json").read_text())
    assert activity["active"] is was_active
    for action in ("start", "stop"):
        assert commands.count(("systemctl", action, service)) == int(was_active)
    assert len(requests) == (3 if was_active else 1)
    assert report["room_health"]["state"] == ("passed" if was_active else "not_checked")
    assert not report["restored"] and not report["counters_passed"]
    assert report["association_after_cleanup"] == list(original)
    assert report["error"] == "RuntimeError: trial unavailable"
    assert report["cleanup_errors"] == ["close native provider: RuntimeError: provider close failed"]
    assert control.apply_frequency.call_count == 2
    assert ("nsenter", "-t", "123", "-n", "ip", "route", "del", "192.0.2.1/32", "dev", "br0") in commands
