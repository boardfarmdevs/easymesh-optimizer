"""Run inside the lab VM, after the step4 regression; --live is required to execute."""

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import signal
import subprocess
import time
import urllib.request
from urllib.error import URLError
import uuid
import re

# The default room of each room set: the lab's own rooms (worlds), with its wired AP
# (worlds-wired), or with the OpenSync pods as well (worlds-pods).
DEFAULT_WORLD = re.compile(r"home-five-agent(?:-(wired|pods))?--private-client-room-walk")


PORT = 55204
CASES = ("lease-expiry", "shutdown", "nonzero-loss")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def command(*arguments, check=True, timeout=10):
    return subprocess.run(arguments, check=check, capture_output=True, text=True, timeout=timeout)


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def verify_loss(phases, counters, payload_bytes):
    require(bool(phases), "missing completed UDP phases")
    for phase in phases:
        require(phase.get("state") == "completed" and not phase.get("cleanup_errors"), "UDP phase did not complete cleanly")
        require(phase.get("source") == "bound_client_wlan0_udp_iperf3" and
                phase.get("source_interface") == "wlan0" and phase.get("port") == PORT and
                phase.get("payload_bytes") == payload_bytes, "wrong traffic provenance")
        for endpoint, rate in (("sender", "bits_per_second"), ("receiver", "goodput_bits_per_second")):
            record = phase[endpoint]
            require(record.get("status") == "complete" and record.get("returncode") == 0 and
                    record.get("source") == "iperf3_" + endpoint + "_json", "incomplete endpoint evidence")
            require(record["seconds"] > 0 and math.isclose(record[rate], record["bytes"] * 8 / record["seconds"],
                    rel_tol=0.001, abs_tol=1), "endpoint byte/rate inconsistency")
        receiver = phase["receiver"]
        require(0 < receiver["lost_packets"] < receiver["packets"] and math.isclose(
            receiver["loss_percent"], 100 * receiver["lost_packets"] / receiver["packets"], abs_tol=0.01),
            "each receiver must measure consistent nonzero, non-total loss")
    sent = sum(phase["sender"]["packets"] for phase in phases)
    delivered = sum(phase["receiver"]["bytes"] for phase in phases) // payload_bytes
    lost = sum(phase["receiver"]["lost_packets"] for phase in phases)
    require(sent == counters["sent"], "sender packets disagree with independent OUTPUT counter")
    require(sum(phase["sender"]["bytes"] for phase in phases) == sent * payload_bytes, "sender payload count mismatch")
    require(sum(phase["receiver"]["bytes"] for phase in phases) == counters["delivered"] * payload_bytes,
            "receiver bytes disagree with independent INPUT counter")
    require(0 < counters["dropped"] and counters["arrived"] == counters["dropped"] + counters["delivered"]
            and counters["arrived"] <= sent, "independent drop ledger is inconsistent")
    tail = sent - sum(phase["receiver"]["packets"] for phase in phases)
    require(0 <= tail <= len(phases) and lost + tail == sent - delivered,
            "receiver sequence loss disagrees with independent counters beyond one terminal datagram per phase")
    return {"passed": True, "scope": "scoped_injection_packet_loss_telemetry_only", "rf_loss_claim": False,
            "aggregation": "independent counters span both bounded playback phases",
            "independent_source": "iptables-legacy mangle packet counters", "counters": counters,
            "independent_missing_packets": sent - delivered, "receiver_lost_packets": lost,
            "unobserved_terminal_packets": tail, "maximum_terminal_packets": len(phases),
            "rate_check": "byte/rate consistency using endpoint duration; no independent rate timebase"}


class Lab:
    def __init__(self, args):
        self.args = args
        self.rdk = args.stack == "rdk"
        self.service = "easymesh-room-service" if self.rdk else "prplmesh-room-service"
        self.gateway = "bpibroadband" if self.rdk else "prpl-controller"
        self.bridge = "brlan0" if self.rdk else "br-lan"
        self.target = "10.0.0.1" if self.rdk else "192.168.77.1"
        self.token = None
        self.sequence = 0
        self.owner = "task5-traffic-" + uuid.uuid4().hex
        self.cleanup_errors = []
        self.service_stop_requested = False

    def request(self, endpoint, body=None, method=None):
        headers = {"Origin": self.args.room_url, "Content-Type": "application/json"}
        if body is not None and "expected_revision" in body:
            headers["If-Match"] = '"world-revision-' + str(body["expected_revision"]) + '"'
        request = urllib.request.Request(self.args.room_url + "/api/demo/" + endpoint,
            data=None if body is None else json.dumps(body).encode(), headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    def state(self):
        return self.request("interactions")

    def acquire(self):
        result = self.request("interactions/lease", {"owner": self.owner, "command_id": str(uuid.uuid4())})
        self.token = result["token"]
        return result

    def renew(self):
        return self.request("interactions/lease", {"token": self.token, "command_id": str(uuid.uuid4())})

    def mutate(self, endpoint, values):
        self.sequence += 1
        return self.request(endpoint, {"token": self.token, "command_id": str(uuid.uuid4()),
            "expected_revision": self.state()["revision"], "client_sequence": self.sequence, **values})

    def wait(self, predicate, seconds, *, renew=False, samples=None, reconnecting=False):
        deadline = time.monotonic() + seconds
        renewal = time.monotonic() + 5
        current = {}
        while time.monotonic() < deadline:
            try:
                current = self.state()
            except (URLError, ConnectionError):
                if not reconnecting:
                    raise
                time.sleep(0.2)
                continue
            if samples is not None:
                samples.append(self.request("current"))
            if predicate(current):
                return current
            if renew and time.monotonic() >= renewal:
                self.renew()
                renewal = time.monotonic() + 5
            time.sleep(0.2)
        raise RuntimeError("timed out: " + json.dumps(current.get("traffic_experiment")))

    @contextmanager
    def namespace(self, container):
        state = json.loads(command("lxc", "query", "/1.0/instances/" + container + "/state").stdout)
        require(state.get("status") == "Running" and type(state.get("pid")) is int and state["pid"] > 1,
                "endpoint is not running")
        descriptor = os.open("/proc/" + str(state["pid"]) + "/ns/net", os.O_RDONLY)
        try:
            require(os.fstat(descriptor).st_ino != os.stat("/proc/self/ns/net").st_ino, "endpoint uses VM host namespace")
            def execute(*arguments, check=True):
                return subprocess.run(["nsenter", "--net=/proc/self/fd/" + str(descriptor), "--", *arguments],
                    check=check, capture_output=True, text=True, timeout=3, pass_fds=(descriptor,))
            yield execute
        finally:
            os.close(descriptor)

    def processes(self):
        result = []
        for path in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                arguments = path.read_bytes().decode(errors="replace").strip("\0").split("\0")
                if (arguments and Path(arguments[0]).name == "iperf3" and "-p" in arguments
                        and arguments[arguments.index("-p") + 1] == str(PORT)):
                    result.append({"pid": int(path.parent.name), "arguments": arguments,
                        "namespace": os.stat(path.parent / "ns/net").st_ino,
                        "start_ticks": (path.parent / "stat").read_text().rsplit(")", 1)[1].split()[19]})
            except (FileNotFoundError, ProcessLookupError):
                continue
        return result

    def audit(self, container):
        result = {"processes": self.processes(), "endpoints": {}}
        for node in (container, self.gateway):
            with self.namespace(node) as execute:
                result["endpoints"][node] = {"rules": {table: execute("iptables-legacy", "-w", "1", "-t", table, "-S").stdout
                    for table in ("filter", "mangle")},
                    "listeners": execute("ss", "-H", "-lntup", "sport", "=", ":" + str(PORT)).stdout}
        return result

    def event_path(self, run_id):
        require(Path(run_id).name == run_id and run_id not in (".", ".."), "invalid run id")
        arguments = json.loads(command("systemctl", "show", self.service, "--property=MainPID", "--value").stdout)
        argv = Path("/proc", str(arguments), "cmdline").read_bytes().decode().strip("\0").split("\0")
        require("--output-root" in argv, "service does not declare its evidence root")
        return Path(argv[argv.index("--output-root") + 1], run_id, "live-events.jsonl")

    def lifecycle(self, operation, container):
        if operation == "lease-expiry":
            lease = self.renew()
            delay = timestamp(lease["expires_at"]) - time.time() - 7.5
            require(delay > 0, "lease too short to schedule expiry during live UDP")
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                time.sleep(max(0, min(0.2, deadline - time.monotonic())))
        current = self.request("current")
        journal = self.event_path(current["run_id"])
        self.mutate("playback", {"action": "play"})
        active = self.wait(lambda state: state["traffic_experiment"]["state"] in ("running", "failed"), 8)
        phase = active["traffic_experiment"]
        require(phase["state"] == "running", "UDP failed before cancellation")
        running = self.audit(container)
        require(len(running["processes"]) == 2, "expected two live UDP processes")
        started = time.monotonic()
        if operation == "shutdown":
            self.service_stop_requested = True
            command("systemctl", "stop", "--no-block", self.service)
            deadline = started + 4
            while self.processes() and time.monotonic() < deadline:
                time.sleep(0.1)
        else:
            stopped = self.wait(lambda state: not state["lease"]["held"] and state["traffic_experiment"]["state"] == "off", 7)
            require(stopped["playback"]["status"] == "paused", "lease expiry did not pause playback")
            self.token = None
        elapsed = time.monotonic() - started
        require(not self.processes(), "UDP processes survived cancellation")
        if operation == "shutdown":
            require(elapsed <= 4, "UDP shutdown exceeded four seconds")
        if operation == "shutdown":
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                status = command("systemctl", "show", self.service, "--property=ActiveState", "--value").stdout.strip()
                if status in ("inactive", "failed"):
                    break
                time.sleep(0.2)
            require(status == "inactive", "service did not finish clean shutdown")
            self.token = None
        events = [json.loads(line) for line in journal.read_text().splitlines()]
        results = [event["payload"] for event in events if event.get("kind") == "traffic.experiment"
                   and event["payload"].get("key") == phase["key"] and event["payload"].get("state") == "cancelled"]
        require(results and not results[-1].get("cleanup_errors"), "missing clean cancellation event for the live phase")
        reason = "interaction.lease.expired" if operation == "lease-expiry" else "rf.restore.completed"
        proof = [event for event in events if event.get("kind") == reason and event["sequence"] > current["sequence"]]
        require(proof, "missing lifecycle event: " + reason)
        if operation == "shutdown":
            require(proof[-1]["payload"].get("verified") is True, "shutdown RF restoration was not verified")
        else:
            cancelled_at = next(event["recorded_at"] for event in events if event.get("kind") == "traffic.experiment"
                and event["payload"] == results[-1])
            require(0 <= timestamp(cancelled_at) - timestamp(lease["expires_at"]) <= 4,
                    "UDP cancellation exceeded four seconds after the actual lease deadline")
        return {"passed": True, "operation": operation, "observed_to_reaped_seconds": elapsed,
                "lease_expires_at": lease["expires_at"] if operation == "lease-expiry" else None,
                "running_audit": running, "phase": results[-1], "lifecycle_events": proof,
                "event_journal": str(journal), "event_journal_sha256": hashlib.sha256(journal.read_bytes()).hexdigest()}

    @contextmanager
    def loss_counters(self, container, payload_bytes):
        with ExitStack() as cleanup:
            sender = cleanup.enter_context(self.namespace(container))
            receiver = cleanup.enter_context(self.namespace(self.gateway))
            addresses = json.loads(sender("ip", "-j", "-4", "address", "show", "dev", "wlan0").stdout)
            sources = [entry["local"] for interface in addresses for entry in interface.get("addr_info", []) if entry.get("scope") == "global"]
            require(len(sources) == 1, "requires one wlan0 source address")
            route = json.loads(sender("ip", "-j", "-4", "route", "get", self.target, "from", sources[0]).stdout)
            require(len(route) == 1 and route[0].get("dev") == "wlan0", "traffic must route through wlan0")
            chain = "t5_" + uuid.uuid4().hex[:16]
            match = ["-s", sources[0] + "/32", "-d", self.target + "/32", "-p", "udp", "--dport", str(PORT),
                     "-m", "length", "--length", str(payload_bytes + 28), "-m", "comment", "--comment", self.owner]
            counter_rule = ["-o", "wlan0", *match]
            jump_rule = ["-i", self.bridge, *match, "-j", chain]
            def firewall(execute, *arguments, check=True):
                return execute("iptables-legacy", "-w", "1", "-t", "mangle", *arguments, check=check)
            def remove(execute, *arguments):
                try:
                    if arguments[0] == "-X" and "-N " + arguments[1] not in firewall(execute, "-S").stdout.splitlines():
                        return
                    if arguments[0] == "-D":
                        present = firewall(execute, "-C", *arguments[1:], check=False)
                        if present.returncode == 1 and not present.stderr.strip():
                            return
                        require(present.returncode == 0, "cannot verify owned rule before removal")
                    firewall(execute, *arguments)
                except Exception as error:
                    self.cleanup_errors.append(str(error))
            require("-N " + chain not in firewall(receiver, "-S").stdout, "injection chain already exists")
            cleanup.callback(remove, receiver, "-X", chain)
            firewall(receiver, "-N", chain)
            cleanup.callback(remove, receiver, "-F", chain)
            firewall(receiver, "-A", chain, "-m", "statistic", "--mode", "nth", "--every", "5", "--packet", "0", "-j", "DROP")
            firewall(receiver, "-A", chain, "-j", "RETURN")
            cleanup.callback(remove, receiver, "-D", "INPUT", *jump_rule)
            firewall(receiver, "-I", "INPUT", *jump_rule)
            cleanup.callback(remove, sender, "-D", "OUTPUT", *counter_rule)
            firewall(sender, "-I", "OUTPUT", *counter_rule)
            def read():
                raw = {"sender": sender("iptables-legacy-save", "-c", "-t", "mangle").stdout,
                       "receiver": receiver("iptables-legacy-save", "-c", "-t", "mangle").stdout}
                def count(endpoint, rule):
                    def matches(line):
                        fields = shlex.split(line)
                        if len(fields) < 3 or fields[1:3] != rule[:2]:
                            return False
                        if "--comment" in rule:
                            return "--comment" in fields and fields[fields.index("--comment") + 1] == self.owner
                        return "-j" in fields and fields[fields.index("-j") + 1] == rule[-1]
                    rows = [line for line in raw[endpoint].splitlines() if matches(line)]
                    require(len(rows) == 1, "missing or ambiguous owned counter")
                    return int(rows[0].split(":", 1)[0].lstrip("["))
                counters = {"sent": count("sender", ["-A", "OUTPUT", *counter_rule]),
                            "arrived": count("receiver", ["-A", "INPUT", *jump_rule]),
                            "dropped": count("receiver", ["-A", chain, "-m", "statistic", "--mode", "nth", "--every", "5", "--packet", "0", "-j", "DROP"]),
                            "delivered": count("receiver", ["-A", chain, "-j", "RETURN"])}
                return {"counters": counters, "raw": raw, "owner": self.owner, "chain": chain,
                        "source": sources[0], "target": self.target, "port": PORT, "payload_bytes": payload_bytes,
                        "injection": "gateway mangle INPUT drops every fifth matching UDP data packet; telemetry only"}
            yield read


def run(args):
    lab = Lab(args)
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"case": args.case, "stack": args.stack, "passed": False, "state": "failed",
              "started_at": datetime.now(timezone.utc).isoformat(), "owner": lab.owner,
              "restoration_errors": lab.cleanup_errors, "live": True}
    initial = None
    before = None
    acquired = False
    samples = []
    try:
        initial = lab.state()
        require(not initial["lease"]["held"] and not initial["recording"]["active"] and
                initial["playback"]["status"] == "paused" and not initial["playback"]["manual_roles"],
                "requires idle, unleased, unedited room")
        require(DEFAULT_WORLD.fullmatch(initial["selected_world"] or "") is not None and initial["playback"]["time_ms"] == 0,
                "requires default room at time zero so restoration is exact")
        bindings = json.loads((args.root / "rooms/bindings/private-client-room-walk.json").read_text())["roles"]
        container = bindings["sta_static_01"]
        before = lab.audit(container)
        require(not before["processes"] and all(not endpoint["listeners"].strip() and
                all(owner not in json.dumps(endpoint["rules"]) for owner in ("room-traffic-", "task5-traffic-"))
                for endpoint in before["endpoints"].values()), "pre-existing traffic resources; refusing interference")
        report["initial_audit"] = before
        report["original_world"] = initial["selected_world"]
        report["source_sha256"] = {name: hashlib.sha256((args.root / name).read_bytes()).hexdigest() for name in (
            "optimizer/room_service/traffic_experiment.py", "optimizer/room_service/interactions.py", "optimizer/optimizer/load_policy.py")}
        lab.acquire()
        acquired = True
        lab.mutate("world/apply", {"world": "traffic-low-high-off"})
        lab.renew()
        if args.case in ("lease-expiry", "shutdown"):
            report["result"] = lab.lifecycle(args.case, container)
        else:
            with lab.loss_counters(container, 1200) as read:
                lab.mutate("playback", {"action": "play"})
                finished = lab.wait(lambda state: state["playback"]["status"] == "completed" and
                                    state["traffic_experiment"]["state"] == "off", 55, renew=True, samples=samples)
                phases = finished["traffic_experiment"]["history"][-2:]
                require(len(phases) == 2 and all(phase.get("state") == "completed" and not phase.get("cleanup_errors")
                        for phase in phases), "bounded UDP workload did not complete both phases")
                report["phases"] = phases
                report["counter_evidence"] = read()
                report["result"] = verify_loss(phases, report["counter_evidence"]["counters"], 1200)
        report["passed"] = report["result"]["passed"]
        report["state"] = "passed" if report["passed"] else report["result"].get("state", "failed")
    except Exception as error:
        report["error"] = str(error)
    finally:
        if lab.service_stop_requested:
            try:
                command("systemctl", "start", lab.service, timeout=180)
                lab.wait(lambda state: state.get("enabled") is True, 120, reconnecting=True)
            except Exception as error:
                lab.cleanup_errors.append("restart: " + str(error))
        if acquired:
            try:
                if not lab.state()["lease"]["held"]:
                    lab.token = None
                    lab.acquire()
                lab.renew()
                lab.mutate("world/apply", {"world": "default"})
            except Exception as error:
                lab.cleanup_errors.append("world restore: " + str(error))
            finally:
                if lab.token is not None:
                    try:
                        lab.request("interactions/lease", {"token": lab.token, "command_id": str(uuid.uuid4())}, "DELETE")
                    except Exception as error:
                        lab.cleanup_errors.append("lease release: " + str(error))
        if before is not None and report.get("original_world"):
            try:
                report["final_audit"] = lab.audit(container)
                require(report["final_audit"] == before, "process, port or firewall state did not return to baseline")
                final = lab.state()
                require(final["selected_world"] == initial["selected_world"] and final["roles"] == initial["roles"] and not final["lease"]["held"]
                        and final["playback"]["status"] == "paused", "room did not return to idle original world")
            except Exception as error:
                lab.cleanup_errors.append("final audit: " + str(error))
        if lab.cleanup_errors:
            report.update(passed=False, state="failed")
        (args.output / "samples.jsonl").write_text("".join(json.dumps(snapshot) + "\n" for snapshot in samples))
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", choices=("prpl", "rdk"), required=True)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--root", type=Path, required=True, help="runtime repository root inside the VM")
    parser.add_argument("--room-url", default="http://127.0.0.1:8891")
    parser.add_argument("--output", type=Path, required=True, help="new evidence directory inside the VM")
    parser.add_argument("--live", action="store_true", help="execute only after parent completes step4 and starts step5 LIVE")
    args = parser.parse_args(argv)
    if not args.live:
        print(json.dumps({"state": "prepared", "live": False, "case": args.case,
            "requires": "step4 regression complete, step5 LIVE authorized, idle unleased VM",
            "scope": "packet-loss telemetry injection only" if args.case == "nonzero-loss" else args.case}))
        return 0
    require(os.geteuid() == 0, "live harness must run as root inside the lab VM")
    def interrupted(signum, _frame):
        raise RuntimeError("interrupted by signal " + str(signum))
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    report = run(args)
    print(json.dumps(report))
    return 0 if report["passed"] else 2 if report["state"] == "unsupported_workload" else 1


if __name__ == "__main__":
    raise SystemExit(main())
