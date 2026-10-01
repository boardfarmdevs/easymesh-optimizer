import importlib.util
import io
import json
from pathlib import Path
import signal
import subprocess
from types import SimpleNamespace

import pytest

from room_service.recovery import _digest


SPEC = importlib.util.spec_from_file_location("counter_manifest_smoke", Path(__file__).with_name("counter-guard-room-smoke.py"))
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)


class Process:
    pid = 12345

    def __init__(self, *, timeout=False, code=0, reap_timeout=False):
        self.returncode = None
        self.timeout = timeout
        self.code = code
        self.reap_timeout = reap_timeout
        self.waits = []
        self.signals = []

    def poll(self):
        return self.returncode

    def send_signal(self, value):
        self.signals.append(value)

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.timeout and (len(self.waits) == 1 or self.reap_timeout):
            raise subprocess.TimeoutExpired("fixture", timeout)
        self.returncode = -signal.SIGKILL if self.timeout else self.code
        return self.returncode


def journal(path, **changes):
    value = {"schema": "easymesh.room-demo.recovery.v1", "run_id": "owned-run",
             "state": "restored", "baseline": [], "paused_clients": [], "client_networks": {}}
    value.update(changes)
    value["checksum_sha256"] = _digest(value)
    path.write_text(json.dumps(value))


def summary(output, **changes):
    directory = output / "runs/owned-run"
    directory.mkdir(parents=True, exist_ok=True)
    value = {"run_id": "owned-run", "outcome": "passed", "restored": True, "error": None}
    value.update(changes)
    (directory / "interactive-summary.json").write_text(json.dumps(value))


def test_grace_exceeds_cli_postflight_and_requires_summary_and_empty_journal(tmp_path):
    path = tmp_path / "recovery.json"
    journal(path)
    summary(tmp_path)
    process = Process()
    result = HELPER.shutdown_manifest(process, Path("room-service"), path, tmp_path)
    assert process.signals == [signal.SIGINT]
    assert process.waits == [180]
    assert result["safe_to_restart"] and result["errors"] == []
    assert not result["forced_kill"]


@pytest.mark.parametrize("pending", [{"state": "restoring"}, {"paused_clients": ["client"]},
                                     {"client_networks": {"client": {"network_id": "0"}}}])
def test_restored_flag_alone_does_not_hide_pending_client_recovery(tmp_path, monkeypatch, pending):
    path = tmp_path / "recovery.json"
    journal(path, **pending)
    summary(tmp_path)
    process = Process()
    calls = []

    def recover(entrypoint, journal_path, output):
        assert process.returncode == 0
        calls.append(journal_path)
        journal(journal_path)
        return HELPER.restored_journal(journal_path)

    monkeypatch.setattr(HELPER, "recover_manifest", recover)
    result = HELPER.shutdown_manifest(process, Path("room-service"), path, tmp_path)
    assert result["safe_to_restart"] and result["recovery_attempted"]
    assert result["errors"] and calls == [path]


def test_forced_kill_reaps_whole_owned_group_before_recovery_and_still_fails(tmp_path, monkeypatch):
    path = tmp_path / "recovery.json"
    journal(path, paused_clients=["client"])
    process = Process(timeout=True)
    actions = []
    monkeypatch.setattr(HELPER.os, "killpg", lambda pid, value: actions.append((pid, value)))

    def recover(entrypoint, journal_path, output):
        assert process.returncode == -signal.SIGKILL
        assert actions == [(process.pid, signal.SIGKILL)]
        journal(journal_path)
        return HELPER.restored_journal(journal_path)

    monkeypatch.setattr(HELPER, "recover_manifest", recover)
    result = HELPER.shutdown_manifest(process, Path("room-service"), path, tmp_path)
    assert process.waits == [180, 10]
    assert result["forced_kill"] and result["safe_to_restart"] and result["errors"]


@pytest.mark.parametrize("failure", ["unreaped", "checksum", "recovery"])
def test_unverified_cleanup_cannot_restart_default(tmp_path, monkeypatch, failure):
    path = tmp_path / "recovery.json"
    journal(path, paused_clients=["client"])
    summary(tmp_path)
    process = Process(timeout=failure == "unreaped", reap_timeout=True)
    monkeypatch.setattr(HELPER.os, "killpg", lambda *_args: None)
    if failure == "checksum":
        path.write_text(path.read_text().replace('"restored"', '"active"'))
    calls = []

    def recover(*arguments):
        calls.append(arguments)
        raise RuntimeError("journal or medium/generation recovery verification failed")

    monkeypatch.setattr(HELPER, "recover_manifest", recover)
    result = HELPER.shutdown_manifest(process, Path("room-service"), path, tmp_path)
    assert not result["safe_to_restart"] and result["errors"]
    assert bool(calls) == (failure != "unreaped")


@pytest.mark.parametrize("changes", [{"outcome": "failed"}, {"restored": False}, {"run_id": "foreign"}])
def test_bad_exit_summary_never_qualifies_even_with_clean_journal(tmp_path, changes):
    path = tmp_path / "recovery.json"
    journal(path)
    summary(tmp_path, **changes)
    result = HELPER.shutdown_manifest(Process(), Path("room-service"), path, tmp_path)
    assert result["safe_to_restart"] == (changes.get("run_id", "owned-run") == "owned-run")
    assert result["errors"]


@pytest.mark.parametrize("failure", [None, "exit", "timeout", "pending_clients"])
def test_supported_recover_command_has_bound_and_must_clear_client_state(tmp_path, monkeypatch, failure):
    path = tmp_path / "recovery.json"
    journal(path, paused_clients=["client"] if failure == "pending_clients" else [])
    process = Process(timeout=failure == "timeout", code=2 if failure == "exit" else 0)
    calls, signals = [], []

    def spawn(command, **options):
        calls.append((command, options))
        return process

    monkeypatch.setattr(HELPER.subprocess, "Popen", spawn)
    monkeypatch.setattr(HELPER.os, "killpg", lambda *args: signals.append(args))
    if failure:
        with pytest.raises((RuntimeError, subprocess.TimeoutExpired)):
            HELPER.recover_manifest(Path("room-service"), path, tmp_path)
    else:
        assert HELPER.recover_manifest(Path("room-service"), path, tmp_path)["state"] == "restored"
    assert calls[0][0][2:] == ["recover", "--recovery-file", str(path)]
    assert calls[0][1]["start_new_session"] is True
    assert process.waits[0] == 180
    assert bool(signals) == (failure == "timeout")


def test_configured_guard_does_not_require_load_applicability_or_invent_evidence():
    flags = {"policy_enabled": True, "counter_guard_enabled": True}
    current = {"optimizer": {"rf_observations": flags, "client_decisions": [
        {"reason": "current_metric_missing", "load_evidence": None},
        {"reason": "threshold_margin_hold_not_met", "current_rcpi": 110}]}}
    assert HELPER.guard_observation(current, flags) == {"counter_guard_enabled": True, "observed_load_decisions": []}
    for bad in ({}, {"counter_guard_enabled": True}, {**flags, "counter_guard_enabled": False},
                {**flags, "counter_guard_enabled": "true"}):
        assert not HELPER.guard_observation(current, bad)["counter_guard_enabled"]
        assert not HELPER.guard_observation({"optimizer": {"rf_observations": bad}}, flags)["counter_guard_enabled"]
    decision = {"reason": "native_load_counter_pressure", "load_evidence": {"counter_guard_enabled": True}}
    current["optimizer"]["client_decisions"] = [decision]
    assert HELPER.guard_observation(current, flags)["observed_load_decisions"] == [decision]
    assert not HELPER.guard_observation(current, {})["counter_guard_enabled"]


@pytest.mark.parametrize("failure", [None, "configuration", "traffic", "shutdown", "recovery", "baseline"])
def test_main_keeps_gates_failure_evidence_and_default_restart_order(tmp_path, monkeypatch, failure):
    output = tmp_path / "run"
    state = {"manifest": False}
    calls = []
    flags = {"policy_enabled": True, "counter_guard_enabled": failure != "configuration"}

    def request(query, **_options):
        suffix = query.full_url.rsplit("/api/demo/", 1)[1]
        count = 10 if state["manifest"] else 20
        if suffix == "current":
            value = {"health": {"healthy": True}, "network": {"clients": [{}] * count},
                     "optimizer": {"mode": "recommend", "actions_used": 0, "rf_observations": flags,
                                   "client_decisions": [{"current_rcpi": 110, "load_evidence": None}]}}
        elif suffix == "rf-observations":
            value = flags
        elif suffix == "interactions/lease":
            value = {"token": "owned"}
        else:
            value = {"revision": 1, "selected_world": "home-five-agent--private-client-room-walk",
                     "playback": {"status": "completed" if state["manifest"] else "paused", "time_ms": 0},
                     "traffic_experiment": {}}
        return io.StringIO(json.dumps(value))

    def command(arguments, **_options):
        calls.append(arguments)
        if "start" in arguments:
            state["manifest"] = False
        return SimpleNamespace(stdout="inactive", returncode=0)

    def spawn(arguments, **options):
        assert options["start_new_session"] is True
        assert arguments[-2:] == ["--recovery-file", str(output / "manifest-recovery.json")]
        calls.append(["manifest"])
        state["manifest"] = True
        return Process()

    def shutdown(*_arguments):
        calls.append(["shutdown"])
        assert json.loads((output / "report.json").read_text())["passed"] is False
        return {"safe_to_restart": failure != "recovery", "errors": [failure] if failure in {"shutdown", "recovery"} else []}

    def baseline(_path):
        if failure == "baseline":
            raise RuntimeError("paused clients remain in original room journal")
        return {"state": "restored"}

    # a lab with the counter-guard room (the RDK lab's layout: gen/ in its repository)
    lab = tmp_path / "repository/gen"
    (lab / "rooms/manifests").mkdir(parents=True)
    (lab / "rooms/bindings").mkdir(parents=True)
    (lab / "rooms/manifests/native-counter-guard-room-profile.json").write_text(json.dumps({
        "bindings": "gen/rooms/bindings/counter-guard.json",
        "hero": {"role": "sta_static_01", "container": "wlan-client"}}))
    (lab / "rooms/bindings/counter-guard.json").write_text(json.dumps({"roles": {"extender_1": "bpiap"}}))
    (lab / "medium").symlink_to(HELPER.LAB / "medium")  # its rooms: the medium next to this checkout
    monkeypatch.setattr(HELPER, "LAB", lab)
    stack = "rdk"
    monkeypatch.setattr(HELPER.sys, "argv", ["helper", "--stack", stack, "--yes-change-lab", "--output", str(output)])
    monkeypatch.setattr(HELPER.os, "geteuid", lambda: 0)
    monkeypatch.setattr(HELPER, "urlopen", request)
    monkeypatch.setattr(HELPER.subprocess, "run", command)
    monkeypatch.setattr(HELPER.subprocess, "Popen", spawn)
    monkeypatch.setattr(HELPER, "shutdown_manifest", shutdown)
    monkeypatch.setattr(HELPER, "restored_journal", baseline)
    monkeypatch.setattr(HELPER, "prepare_subject", lambda *_args: None)
    monkeypatch.setattr(HELPER, "restore_subject", lambda *_args: None)
    monkeypatch.setattr(HELPER.ROOMS, "traffic_errors", lambda *_args: ["cancelled"] if failure == "traffic" else [])
    result = HELPER.main()
    report = json.loads((output / "report.json").read_text())
    assert result == int(failure is not None)
    assert report["passed"] == (failure is None)
    assert any("start" in command for command in calls) == (failure not in {"recovery", "baseline"})
    assert report["restored_default"] == (failure not in {"recovery", "baseline"})
    if failure != "baseline":
        assert report["observed_load_decisions"] == []
        assert report["guard_enabled_in_decisions"] is False
        assert report["counter_guard_enabled"] == (failure != "configuration")
    if failure is None:
        assert calls.index(["shutdown"]) < next(index for index, command in enumerate(calls) if "start" in command)


def test_traffic_fixture_requires_one_exact_native_ssid_frequency_ap():
    interface = "Interface wlan1\n addr 02:00:00:00:01:00\n ssid private_ssid\n type AP\n channel 36 (5180 MHz)\n"
    assert HELPER.fixture_bssid(interface, "private_ssid", 5180) == "02:00:00:00:01:00"
    for invalid in (interface.replace("type AP", "type managed"), interface * 2,
                    interface.replace("5180", "2437"), interface.replace("private_ssid", "iot_ssid")):
        with pytest.raises(RuntimeError, match="one native AP"):
            HELPER.fixture_bssid(invalid, "private_ssid", 5180)


