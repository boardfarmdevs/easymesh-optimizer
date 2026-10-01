import importlib.util
import json
from pathlib import Path
import select
import subprocess
import sys


SCRIPT = Path(__file__).with_name("room-feature-host-monitor.py")
SPEC = importlib.util.spec_from_file_location("host_monitor", SCRIPT)
MONITOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MONITOR)


def test_cpu_sampling_requires_an_interval_and_excludes_idle_and_iowait():
    assert MONITOR.cpu_busy([10, 0, 0, 20, 5, 0, 0, 0], None) is None
    assert MONITOR.cpu_busy([10, 0, 0, 20, 5, 0, 0, 0], [0] * 8) == 28.57
    assert MONITOR.cpu_busy([0] * 8, [0] * 8) is None


def test_process_parser_handles_parentheses_and_missing_processes():
    fields = ["S"] + ["0"] * 21
    fields[11], fields[12], fields[19], fields[21] = "12", "3", "100", "8"
    sample = MONITOR.process_stat("42", "42 (worker ) child) " + " ".join(fields))
    assert sample["name"] == "worker ) child"
    assert sample["cpu_ticks"] == 15
    assert sample["started_ticks"] == 100
    assert MONITOR.process_stat("42", None) is None
    assert MONITOR.process_stat("42", "42 (short) S") is None


def test_process_usage_rejects_pid_reuse_and_counter_reset():
    old = {"pid": 42, "name": "qemu", "started_ticks": 100, "cpu_ticks": 20}
    current = {**old, "cpu_ticks": 170}
    rows = MONITOR.process_usage({(42, 100): current}, {(42, 100): old}, 1, 100)
    assert rows[0]["cpu_percent_one_core"] == 150
    assert MONITOR.process_usage({(42, 200): current}, {(42, 100): old}, 1, 100) == []
    assert MONITOR.process_usage({(42, 100): old}, {(42, 100): current}, 1, 100) == []
    assert MONITOR.process_usage({(42, 100): current}, {(42, 100): old}, 0, 100) == []


def test_owned_sampler_produces_multiple_samples_and_exits_on_stdin_close():
    process = subprocess.Popen([sys.executable, "-u", str(SCRIPT), "--interval", ".1",
                                "--duration", "30", "--watch-stdin"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        for index in range(2):
            assert select.select([process.stdout], [], [], 3)[0]
            sample = json.loads(process.stdout.readline())
            assert sample["available_memory_kib"] > 0
            assert sample["per_cpu_busy_percent"]
            assert "intel_pstate_no_turbo" in sample
            assert isinstance(sample["rapl_power_limits_uw"], dict)
            assert (sample["cpu_busy_percent"] is None) == (index == 0)
        process.stdin.close()
        assert process.wait(timeout=3) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        process.stdout.close()
