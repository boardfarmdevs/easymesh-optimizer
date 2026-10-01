from __future__ import annotations

import copy
from contextlib import ExitStack, contextmanager, nullcontext
import fcntl
import ipaddress
import json
import math
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid

from optimizer.band_scan import NativeBandScanner
from wmdcfg.traffic_profile import validate_phase


UDP_PORT = 55204


def phase_at(profile, time_ms):
    if profile:
        return next(((index, phase) for index, phase in enumerate(profile["phases"])
                     if phase["start_ms"] <= time_ms < phase["end_ms"]), None)
    return None


def packet_counts(output):
    match = re.search(r"(\d+) packets transmitted,\s*(\d+) (?:packets )?received", output)
    return ({"transmitted_packets": int(match[1]), "received_echo_replies": int(match[2])}
            if match else {"transmitted_packets": None, "received_echo_replies": None})


class TrafficCancelled(RuntimeError):
    pass


def udp_endpoint_record(output, sender):
    source = "iperf3_sender_json" if sender else "iperf3_receiver_json"
    rate_field = "bits_per_second" if sender else "goodput_bits_per_second"
    empty = {"status": "missing", "source": source, "bytes": None, "packets": None,
             "seconds": None, rate_field: None}
    if not sender:
        empty.update(lost_packets=None, loss_percent=None)
    if not output.strip():
        return empty
    try:
        document = json.loads(output)
        setup = document["start"]["test_start"]
        if setup["protocol"] != "UDP" or setup["num_streams"] != 1 or setup["reverse"] != 0:
            raise ValueError("expected one forward UDP stream")
        end = document.get("end", {})
        summary = end.get("sum_sent" if sender else "sum_received")
        provenance = "end.sum_sent" if sender else "end.sum_received"
        if summary is None:
            rows = [interval["sum"] for interval in document.get("intervals", [])]
            if not rows:
                return {**empty, "error": str(document.get("error", "endpoint summary unavailable"))[:500]}
            previous_end = 0
            for row in rows:
                if (row.get("sender") is not sender or row.get("omitted") is not False
                        or any(type(row[field]) not in (int, float) or not math.isfinite(row[field])
                               for field in ("start", "end", "seconds"))
                        or any(type(row[field]) is not int or row[field] < 0 for field in ("bytes", "packets"))
                        or (not sender and type(row["lost_packets"]) is not int)
                        or not math.isclose(row["start"], previous_end, abs_tol=0.001)
                        or row["seconds"] <= 0
                        or not math.isclose(row["end"] - row["start"], row["seconds"], abs_tol=0.001)):
                    raise ValueError("incomplete or mismatched endpoint intervals")
                previous_end = row["end"]
            summary = {field: sum(row[field] for row in rows)
                       for field in ("bytes", "packets", "seconds")}
            summary["sender"] = sender
            summary["bits_per_second"] = summary["bytes"] * 8 / summary["seconds"]
            if not sender:
                summary["lost_packets"] = sum(row["lost_packets"] for row in rows)
                summary["lost_percent"] = (100 * summary["lost_packets"] / summary["packets"]
                                           if summary["packets"] else 0)
            provenance = "intervals.sum"
        fields = ("bytes", "packets", "seconds", "bits_per_second")
        if not sender:
            fields += ("lost_packets", "lost_percent")
        if (summary.get("sender") is not sender
                or any(type(summary[field]) not in (int, float) or not math.isfinite(summary[field])
                       or summary[field] < 0 for field in fields)
                or any(type(summary[field]) is not int for field in ("bytes", "packets"))
                or summary["seconds"] <= 0
                or not math.isclose(summary["bits_per_second"], summary["bytes"] * 8 / summary["seconds"],
                                    rel_tol=0.001, abs_tol=1)):
            raise ValueError("invalid endpoint counters or direction")
        record = {**empty, "status": "partial" if document.get("error") or not end else "complete",
                  "summary": provenance, "bytes": summary["bytes"], "packets": summary["packets"],
                  "seconds": summary["seconds"], rate_field: summary["bits_per_second"]}
        if not sender:
            if (type(summary["lost_packets"]) is not int or summary["lost_packets"] > summary["packets"]
                    or summary["lost_percent"] > 100
                    or not math.isclose(summary["lost_percent"], 100 * summary["lost_packets"] / summary["packets"]
                                        if summary["packets"] else 0, abs_tol=0.01)):
                raise ValueError("invalid receiver loss counters")
            record.update(lost_packets=summary["lost_packets"], loss_percent=summary["lost_percent"])
        if document.get("error"):
            record["error"] = str(document["error"])[:500]
        return record
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
        return {**empty, "status": "invalid", "error": str(error)[:500]}


def _private_address(target):
    address = ipaddress.ip_address(target)
    if address.version != 4 or not any(address in ipaddress.ip_network(network)
                                      for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
        raise ValueError("traffic target must be the configured private IPv4 lab gateway")
    return str(address)


def namespace_udp(container, target, phase, remaining_ms, *, admit, register, running):
    validate_phase(phase)
    if phase.get("mode") != "udp":
        raise ValueError("explicit UDP phase required")
    NativeBandScanner._validate_container(container)
    target = _private_address(target)
    gateway, bridge = (("prpl-controller", "br-lan") if container.startswith("prpl-client-")
                       else ("bpibroadband", "brlan0"))
    deadline = time.monotonic() + min(remaining_ms, phase["end_ms"] - phase["start_ms"], 20000) / 1000
    processes, outputs, cleanup_errors = [], {}, []
    result = {"gateway_container": gateway, "port": UDP_PORT,
              "sender": udp_endpoint_record("", True), "receiver": udp_endpoint_record("", False)}

    def command(arguments, descriptor=None, *, cleanup=False, check=True):
        timeout = 1 if cleanup else min(1, deadline - time.monotonic())
        if timeout <= 0:
            raise TrafficCancelled("UDP phase expired during setup")
        if not cleanup:
            with admit():
                pass
        prefix = ["nsenter", f"--net=/proc/self/fd/{descriptor}", "--"] if descriptor is not None else []
        return subprocess.run(prefix + arguments, check=check, capture_output=True, text=True,
                              timeout=timeout, pass_fds=() if descriptor is None else (descriptor,))

    def remove_rule(descriptor, chain, rule):
        try:
            present = command(["iptables-legacy", "-w", "1", "-C", chain, *rule], descriptor,
                              cleanup=True, check=False)
            if present.returncode == 0:
                command(["iptables-legacy", "-w", "1", "-D", chain, *rule], descriptor, cleanup=True)
            elif present.returncode != 1 or present.stderr.strip():
                raise ValueError("could not verify removal of owned traffic firewall rule")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            cleanup_errors.append(str(error))

    def reap():
        for process in processes:
            TrafficExperiment._signal(process, signal.SIGINT)
        for process in processes:
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                TrafficExperiment._signal(process, signal.SIGKILL)
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    cleanup_errors.append("owned UDP process could not be reaped")
        for endpoint, process in zip(("receiver", "sender"), processes):
            outputs[endpoint].seek(0)
            record = udp_endpoint_record(outputs[endpoint].read(262144), endpoint == "sender")
            if process.returncode != 0 and record["status"] == "complete":
                record["status"] = "partial"
            result[endpoint] = {**record, "returncode": process.returncode}

    try:
        with ExitStack() as cleanup:
            namespaces = []
            host = os.stat("/proc/self/ns/net").st_ino
            for node in (container, gateway):
                state = json.loads(command(["lxc", "query", f"/1.0/instances/{node}/state"]).stdout)
                if state.get("status") != "Running" or type(state.get("pid")) is not int or state["pid"] <= 1:
                    raise ValueError("traffic endpoint container is not running")
                descriptor = os.open(f"/proc/{state['pid']}/ns/net", os.O_RDONLY)
                cleanup.callback(os.close, descriptor)
                if os.fstat(descriptor).st_ino in [host, *[os.fstat(item).st_ino for item in namespaces]]:
                    raise ValueError("traffic requires distinct client and gateway container namespaces")
                namespaces.append(descriptor)
            client_ns, gateway_ns = namespaces
            lock = os.open(f"/run/lock/room-traffic-{os.fstat(gateway_ns).st_ino}-{UDP_PORT}.lock",
                           os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            cleanup.callback(os.close, lock)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            client_addresses = json.loads(command(["ip", "-j", "-4", "address", "show"], client_ns).stdout)
            sources = [entry["local"] for interface in client_addresses if interface["ifname"] == "wlan0"
                       for entry in interface.get("addr_info", []) if entry.get("family") == "inet"
                       and entry.get("scope") == "global"]
            if len(sources) != 1 or any(entry.get("local") == target for interface in client_addresses
                                         for entry in interface.get("addr_info", [])):
                raise ValueError("traffic requires one wlan0 IPv4 source and a nonlocal gateway")
            source = _private_address(sources[0])
            gateway_addresses = json.loads(command(["ip", "-j", "-4", "address", "show"], gateway_ns).stdout)
            owners = [interface["ifname"] for interface in gateway_addresses
                      for entry in interface.get("addr_info", []) if entry.get("local") == target]
            if owners != [bridge]:
                raise ValueError("configured gateway must belong only to the gateway LAN bridge")
            route = json.loads(command(["ip", "-j", "-4", "route", "get", target, "from", source], client_ns).stdout)
            if len(route) != 1 or route[0].get("dev") != "wlan0" or route[0].get("type") == "local":
                raise ValueError("traffic route must use wlan0 without a management or loopback bypass")
            help_text = command(["iperf3", "--help"]).stdout
            bind_device = "--bind-dev" in help_text
            if command(["ss", "-H", "-lntup", "sport", "=", f":{UDP_PORT}"], gateway_ns).stdout.strip():
                raise ValueError("dedicated UDP experiment port is already occupied")
            owner = "room-traffic-" + uuid.uuid4().hex
            for descriptor, chain in ([(client_ns, "OUTPUT")] if not bind_device else []) + (
                    [(gateway_ns, "INPUT")] if gateway == "bpibroadband" else []):
                for protocol in ("tcp", "udp"):
                    rule = ["-s", source + "/32", "-d", target + "/32", "-p", protocol,
                            "--dport", str(UDP_PORT), "-m", "comment", "--comment", owner]
                    rule += (["!", "-o", "wlan0", "-j", "REJECT"] if chain == "OUTPUT"
                             else ["-i", bridge, "-j", "ACCEPT"])
                    try:
                        command(["iptables-legacy", "-w", "1", "-I", chain, *rule], descriptor)
                    except subprocess.TimeoutExpired:
                        try:
                            present = command(["iptables-legacy", "-w", "1", "-C", chain, *rule], descriptor,
                                              cleanup=True, check=False)
                        except (OSError, subprocess.SubprocessError):
                            cleanup_errors.append("could not determine whether timed-out traffic rule was inserted")
                            raise
                        if present.returncode == 0:
                            cleanup.callback(remove_rule, descriptor, chain, rule)
                        elif present.returncode != 1 or present.stderr.strip():
                            cleanup_errors.append("could not verify timed-out traffic rule insertion")
                        raise
                    cleanup.callback(remove_rule, descriptor, chain, rule)
                    with admit():
                        pass
            for endpoint in ("sender", "receiver"):
                outputs[endpoint] = cleanup.enter_context(tempfile.TemporaryFile(mode="w+"))
            cleanup.callback(reap)

            def launch(arguments, descriptor, endpoint):
                with admit():
                    process = subprocess.Popen(["nsenter", f"--net=/proc/self/fd/{descriptor}", "--", *arguments],
                                               stdin=subprocess.DEVNULL, stdout=outputs[endpoint], stderr=subprocess.STDOUT,
                                               start_new_session=True, pass_fds=(descriptor,))
                    processes.append(process)
                    register(process)
                return process

            server = launch(["iperf3", "-4", "-s", "-1", "-B", target, "-p", str(UDP_PORT), "-J", "-i", "1"],
                            gateway_ns, "receiver")
            ready_deadline = min(deadline, time.monotonic() + 1)
            while True:
                if server.poll() is not None:
                    raise ValueError("UDP receiver exited before listening")
                listeners = command(["ss", "-H", "-lntp", "sport", "=", f":{UDP_PORT}"], gateway_ns).stdout
                if re.search(rf"\bpid={server.pid},", listeners):
                    break
                if time.monotonic() >= ready_deadline:
                    raise ValueError("owned UDP receiver did not become ready")
                time.sleep(0.05)
            seconds = math.floor(deadline - time.monotonic() - 0.75)
            if seconds < 1:
                raise TrafficCancelled("insufficient phase time for bounded UDP and result collection")
            arguments = ["iperf3", "-4", "-c", target, "-B", source, "-p", str(UDP_PORT), "-u",
                         "-b", str(round(phase["offered_mbps"] * 1000000)), "-l", str(phase["payload_bytes"]),
                         "-t", str(seconds), "-P", "1", "-J", "-i", "1", "--connect-timeout", "500"]
            if bind_device:
                arguments += ["--bind-dev", "wlan0"]
            result.update(source_address=source, source_interface="wlan0", requested_duration_seconds=seconds,
                          source_guard="bind_device" if bind_device else "owned_iptables_output")
            client = launch(arguments, client_ns, "sender")
            with admit():
                pass
            running(result)
            while client.poll() is None or server.poll() is None:
                with admit():
                    pass
                if time.monotonic() >= deadline:
                    raise TrafficCancelled("UDP phase deadline reached")
                if client.poll() not in (None, 0) or server.poll() not in (None, 0):
                    break
                time.sleep(0.05)
            reap()
            result["state"] = ("completed" if all(result[endpoint]["status"] == "complete"
                                                  and result[endpoint]["returncode"] == 0
                                                  for endpoint in ("sender", "receiver")) else "failed")
    except TrafficCancelled as error:
        result.update(state="cancelled", error=str(error))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as error:
        result.update(state="failed", error=str(error))
    if cleanup_errors:
        result.update(state="failed", cleanup_errors=cleanup_errors, error="UDP resource cleanup failed")
    return result


def namespace_ping(container, target, phase, remaining_ms, *, admit=nullcontext):
    deadline = time.monotonic() + remaining_ms / 1000
    NativeBandScanner._validate_container(container)
    address = ipaddress.ip_address(target)
    if address.version != 4 or not any(address in ipaddress.ip_network(network)
                                       for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
        raise ValueError("traffic target must be the configured private IPv4 lab gateway")
    state = subprocess.run(["lxc", "query", f"/1.0/instances/{container}/state"], check=True,
                           capture_output=True, text=True, timeout=3)
    namespace_pid = json.loads(state.stdout)["pid"]
    if type(namespace_pid) is not int or namespace_pid <= 1:
        raise ValueError("traffic source container is not running")
    namespace_fd = os.open(f"/proc/{namespace_pid}/ns/net", os.O_RDONLY)
    try:
        seconds = min(20, deadline - time.monotonic())
        if seconds <= 0:
            raise TrafficCancelled("traffic phase expired while resolving its container")
        packets = max(1, math.floor(seconds * phase["packets_per_second"]))
        command = ["nsenter", f"--net=/proc/self/fd/{namespace_fd}", "--", "ping", "-n", "-q",
                   "-I", "wlan0", "-i", str(1 / phase["packets_per_second"]),
                   "-s", str(phase["payload_bytes"]), "-c", str(packets),
                   "-w", str(math.ceil(seconds)), str(address)]
        with admit():
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, start_new_session=True, pass_fds=(namespace_fd,))
        return process, packets
    finally:
        os.close(namespace_fd)


class TrafficExperiment:
    def __init__(self, target, publish, *, launcher=namespace_ping, udp_runner=namespace_udp):
        self.target = target
        self.publish = publish
        self.launcher = launcher
        self.udp_runner = udp_runner
        self._condition = threading.Condition()
        self._desired = None
        self._version = 0
        self._closed = False
        self._thread = None
        self._state = {"state": "idle", "history": []}
        self._process = None
        self._udp_processes = []
        self._udp_cleanup_failed = False

    def snapshot(self):
        with self._condition:
            return copy.deepcopy(self._state)

    def sync(self, key, phase=None, container=None, remaining_ms=0, *, complete_through=None):
        with self._condition:
            if self._closed:
                return
            if key is not None and self._desired and self._desired["key"] == key:
                return
            if key is None and self._desired is None:
                return
            if self._desired is not None:
                natural_end = (complete_through is not None
                               and self._desired["key"][:2] == complete_through[:2]
                               and self._desired["phase"]["end_ms"] <= complete_through[2])
                self._stop_reason(self._desired, self._version,
                                  "phase_end" if natural_end else "cancelled")
            self._version += 1
            if self._process is not None:
                self._signal(self._process, signal.SIGINT)
            for process in self._udp_processes:
                self._signal(process, signal.SIGINT)
            self._desired = ({"key": key, "phase": copy.deepcopy(phase), "container": container,
                              "deadline": time.monotonic() + remaining_ms / 1000}
                             if key is not None else None)
            if self._thread is None and key is not None:
                self._thread = threading.Thread(target=self._worker, name="room-traffic", daemon=True)
                self._thread.start()
            self._condition.notify_all()

    def _stop_reason(self, job, version, reason=None):
        with self._condition:
            if job.get("stop_reason") is None:
                if time.monotonic() >= job["deadline"]:
                    reason = "phase_end"
                elif self._closed or self._version != version:
                    reason = "cancelled"
                if reason is not None:
                    job["stop_reason"] = reason
            return job.get("stop_reason")

    @staticmethod
    def _signal(process, signum):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass

    @contextmanager
    def _admit(self, version, deadline):
        with self._condition:
            if self._closed or self._version != version or time.monotonic() >= deadline:
                raise TrafficCancelled("traffic phase no longer owns the playback")
            yield

    def _report(self, state, result=None):
        with self._condition:
            history = self._state["history"]
            if result is not None:
                history = (history + [result])[-8:]
            self._state = {"world_sha256": self._state.get("world_sha256"), **state, "history": history}
            payload = copy.deepcopy(self._state)
        self.publish(payload)

    def _worker(self):
        handled = -1
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._version != handled)
                if self._closed:
                    return
                handled = self._version
                job = self._desired
            if job is None:
                self._report({"state": "off"})
                continue
            phase = job["phase"]
            if phase.get("mode") == "udp":
                self._udp_job(job, handled)
                continue
            detail = {"key": list(job["key"]), "world_sha256": job["key"][0], "role": phase["role"],
                      "source": "bound_client_wlan0_icmp_echo", "target": self.target,
                      "requested_packets_per_second": phase["packets_per_second"],
                      "payload_bytes": phase["payload_bytes"], "transmitted_packets": None,
                      "received_echo_replies": None}
            process = None
            started = time.monotonic()
            try:
                remaining_ms = max(0, round((job["deadline"] - started) * 1000))
                if not remaining_ms:
                    state = "cancelled" if self._stop_reason(job, handled) == "cancelled" else "expired"
                    self._report({**detail, "state": state}, {**detail, "state": state})
                    continue
                process, requested = self.launcher(job["container"], self.target, phase, remaining_ms,
                                                   admit=lambda: self._admit(handled, job["deadline"]))
                with self._condition:
                    self._process = process
                    stopped = self._stop_reason(job, handled)
                if stopped:
                    self._signal(process, signal.SIGINT)
                if not stopped:
                    self._report({**detail, "state": "running", "requested_packets": requested})
                while process.poll() is None:
                    with self._condition:
                        if self._stop_reason(job, handled) is None:
                            self._condition.wait(timeout=min(0.1, max(0, job["deadline"] - time.monotonic())))
                            continue
                    self._signal(process, signal.SIGINT)
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        self._signal(process, signal.SIGKILL)
                    break
                stopped = self._stop_reason(job, handled, "completed")
                output, _unused = process.communicate(timeout=2)
                counts = packet_counts(output)
                result = {**detail, **counts, "requested_packets": requested,
                          "elapsed_seconds": round(time.monotonic() - started, 3),
                          "returncode": process.returncode,
                          "state": "cancelled" if stopped == "cancelled" else
                                   ("completed" if counts["transmitted_packets"] is not None else "failed")}
                if counts["transmitted_packets"] is None:
                    result["error"] = output[-500:]
                self._report(result, result)
            except TrafficCancelled:
                state = "expired" if self._stop_reason(job, handled) == "phase_end" else "cancelled"
                self._report({**detail, "state": state}, {**detail, "state": state})
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                self._report({**detail, "state": "failed", "error": str(error)},
                             {**detail, "state": "failed", "error": str(error)})
            finally:
                if process is not None and process.poll() is None:
                    self._signal(process, signal.SIGKILL)
                    process.communicate(timeout=2)
                with self._condition:
                    self._process = None

    def _udp_job(self, job, version):
        phase = job["phase"]
        detail = {"key": list(job["key"]), "world_sha256": job["key"][0], "role": phase["role"],
                  "mode": "udp", "source": "bound_client_wlan0_udp_iperf3", "target": self.target,
                  "requested_offered_mbps": phase["offered_mbps"], "payload_bytes": phase["payload_bytes"],
                  "sender": udp_endpoint_record("", True), "receiver": udp_endpoint_record("", False)}
        started = time.monotonic()
        try:
            if self._udp_cleanup_failed:
                raise ValueError("UDP disabled after resource cleanup failure; operator cleanup required")
            result = self.udp_runner(job["container"], self.target, phase,
                                     max(0, round((job["deadline"] - started) * 1000)),
                                     admit=lambda: self._admit(version, job["deadline"]),
                                     register=self._udp_processes.append,
                                     running=lambda value: self._report({**detail, **value, "state": "running"}))
            if result.get("cleanup_errors"):
                self._udp_cleanup_failed = True
        except TrafficCancelled as error:
            result = {"state": "cancelled", "error": str(error)}
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            result = {"state": "failed", "error": str(error)}
        finally:
            with self._condition:
                self._udp_processes = []
        stopped = self._stop_reason(job, version, "completed")
        if result.get("state") != "failed":
            if stopped == "cancelled":
                result["state"] = "cancelled"
            elif stopped == "phase_end" and result.get("state") == "cancelled":
                result["state"] = ("completed" if all(
                    result.get(endpoint, {}).get("status") == "complete"
                    and result[endpoint].get("returncode") == 0
                    for endpoint in ("sender", "receiver")) else "failed")
        result = {**detail, **result, "elapsed_seconds": round(time.monotonic() - started, 3)}
        self._report(result, result)

    def close(self):
        with self._condition:
            if self._desired is not None:
                self._stop_reason(self._desired, self._version, "cancelled")
            self._closed = True
            self._version += 1
            for process in self._udp_processes:
                self._signal(process, signal.SIGINT)
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=12)
            if thread.is_alive():
                raise RuntimeError("bounded room traffic worker did not stop")
