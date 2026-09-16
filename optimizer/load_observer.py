from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

from .model import BssLoadObservation, ClientActivityObservation, format_time


TRAFFIC_COUNTERS = ("bytes_sent", "bytes_received", "packets_sent", "packets_received",
                    "tx_packet_errors", "rx_packet_errors", "retransmissions")


def traffic_rates(current, previous, interval, *, byte_counter_unit_bytes=1):
    if not .05 <= interval <= 10:
        return None
    if byte_counter_unit_bytes not in (1, 1024, 1024 * 1024):
        raise ValueError("unsupported byte counter unit")
    deltas = {}
    for name in TRAFFIC_COUNTERS:
        if any(type(sample.get(name)) is not int or not 0 <= sample[name] < 1 << 32
               for sample in (current, previous)):
            continue
        value = (current[name] - previous[name]) % (1 << 32)
        if value < 1 << 31:
            deltas[name] = value
    if not all(name in deltas for name in ("packets_sent", "packets_received")):
        return None
    packets = deltas["packets_sent"] + deltas["packets_received"]
    if packets > interval * 1000000:
        return None
    return {
        "packets_per_second": packets / interval,
        "bytes_per_second": (((deltas["bytes_sent"] + deltas["bytes_received"])
                              * byte_counter_unit_bytes) / interval
                             if all(name in deltas for name in ("bytes_sent", "bytes_received")) else None),
        "retries_per_second": (deltas["retransmissions"] / interval
                               if "retransmissions" in deltas else None),
        "errors_per_second": ((deltas["tx_packet_errors"] + deltas["rx_packet_errors"]) / interval
                              if all(name in deltas for name in ("tx_packet_errors", "rx_packet_errors")) else None),
        "tx_errors_per_second": (deltas["tx_packet_errors"] / interval
                                 if "tx_packet_errors" in deltas else None),
        "rx_errors_per_second": (deltas["rx_packet_errors"] / interval
                                 if "rx_packet_errors" in deltas else None),
        "byte_counter_unit_bytes": byte_counter_unit_bytes,
        "deltas": deltas,
    }


def inventory(raw):
    topology = (raw or {}).get("topology", {})
    bsses = {}
    parents = {}
    roots = set()
    if "nodes" in topology:
        nodes = {row["id"].lower() for row in topology["nodes"]}
        for edge in topology.get("edges", []):
            child, parent = edge["to"].lower(), edge["from"].lower()
            parents[child] = (parent, 0 if edge.get("mediaType") == "Ethernet" else 1)
        roots = nodes - set(parents)
        for row in (raw or {}).get("bsses", {}).get("bsses", []):
            bsses[row["bssid"].lower()] = row
    else:
        for device in topology.get("devices", []):
            identity = device["id"].lower()
            backhaul = device.get("backhaul", {})
            parent = backhaul.get("parent_id")
            if parent:
                parents[identity] = (parent.lower(), 0 if backhaul.get("type") == "Ethernet" else 1)
            else:
                roots.add(identity)
            for radio in device.get("radios", []):
                for bss in radio.get("bsses", []):
                    bsses[bss["bssid"].lower()] = {"device_id": identity, "radio_id": radio["id"],
                                                  "channel": radio.get("channel"), "ssid": bss.get("ssid")}
    hops = {identity: 0 for identity in roots}
    for _iteration in range(len(parents)):
        for child, (parent, cost) in parents.items():
            if parent in hops:
                hops[child] = hops[parent] + cost
    return bsses, hops


def provenance(path, now_ns):
    try:
        value = json.loads(path.read_text())
        if (value.get("schema") != "easymesh.rf-survey-bridge.v1"
                or value.get("source") != "wmediumd-modeled-airtime"
                or not 0 <= now_ns - value["recorded_monotonic_ns"] <= 1000000000):
            return None
        return str(value["instance_id"])
    except (OSError, ValueError, TypeError, KeyError):
        return None


class NativeLoadProvider:
    def __init__(self, controller=None, *, provenance_path=Path("/run/wmdcfg-survey.json"),
                 report_observer=None, byte_counter_unit_bytes=1):
        if byte_counter_unit_bytes not in (1, 1024, 1024 * 1024):
            raise ValueError("unsupported byte counter unit")
        self.provenance_path = provenance_path
        self.byte_counter_unit_bytes = byte_counter_unit_bytes
        self.loads = {}
        self.traffic = {}
        self.contexts = {}
        self.client_owners = {}
        self.epoch = None
        self.floor_ns = 0
        self.error = None
        self.lock = threading.Lock()
        self.child = None
        self.threads = []
        self.ready = threading.Event()
        self.report_observer = report_observer
        if controller is not None:
            state = json.loads(subprocess.check_output(
                ["lxc", "query", f"/1.0/instances/{controller}/state"], timeout=10, text=True))
            receiver_args = ["--watch-stdin"]
            if controller == "prpl-controller":
                receiver_args += ["--broker-socket", f"/proc/{state['pid']}/root/tmp/beerocks/uds_broker"]
            self.child = subprocess.Popen(
                ["nsenter", "-t", str(state["pid"]), "-n", sys.executable,
                 str(Path(__file__).with_name("load_capture.py")), *receiver_args],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for stream, target in ((self.child.stdout, self._read), (self.child.stderr, self._errors)):
                thread = threading.Thread(target=target, args=(stream,), daemon=True)
                thread.start()
                self.threads.append(thread)
            if not self.ready.wait(5) or self.error:
                self.close()
                raise RuntimeError(self.error or "native load receiver did not become ready")

    def _read(self, stream):
        try:
            for line in stream:
                value = json.loads(line)
                if value.get("kind") == "ready":
                    self.ready.set()
                elif value.get("kind") == "ap_metrics":
                    if self.report_observer is not None:
                        self.report_observer(value)
                    self.ingest(value)
                else:
                    raise ValueError("unexpected native load receiver record")
        except (ValueError, TypeError, KeyError) as error:
            self.error = str(error)
        finally:
            self.error = self.error or "native load receiver closed"
            self.ready.set()

    def _errors(self, stream):
        for line in stream:
            self.error = line.strip()[:1024]

    def ingest(self, report):
        with self.lock:
            source = report["source"]
            timestamp = report["monotonic_ns"]
            if timestamp < self.floor_ns:
                return
            for row in report["loads"]:
                previous = self.loads.get((source, row["bssid"]))
                if previous and (timestamp <= previous["monotonic_ns"]
                                 or report["received_at"] <= previous["received_at"]):
                    continue
                self.loads[(source, row["bssid"])] = {**row, "source": source,
                    "transport": report.get("transport", "ieee1905-ethernet"),
                    "monotonic_ns": timestamp, "received_at": report["received_at"]}
            for row in report["traffic"]:
                key = (source, row["sta_mac"])
                previous = self.traffic.get(key)
                if previous and (timestamp <= previous["monotonic_ns"]
                                 or report["received_at"] <= previous["received_at"]):
                    continue
                interval = (timestamp - previous["monotonic_ns"]) / 1e9 if previous else 0
                rates = None
                if (previous and .05 <= interval <= 10
                        and previous["transport"] == report.get("transport", "ieee1905-ethernet")):
                    rates = traffic_rates(row, previous, interval,
                                          byte_counter_unit_bytes=self.byte_counter_unit_bytes)
                self.traffic[key] = {**row, "monotonic_ns": timestamp,
                                     "interval_started_monotonic_ns": previous["monotonic_ns"] if previous else None,
                                     "transport": report.get("transport", "ieee1905-ethernet"),
                                     "received_at": report["received_at"], "interval": interval, "rates": rates}
            if len(self.loads) > 256 or len(self.traffic) > 1024:
                self.error = "native load inventory budget exceeded"
                self.loads.clear()
                self.traffic.clear()

    def observe_owners(self, clients, raw, *, now_ns=None):
        """Observe a complete controller roster before candidate work; never backdate ownership."""
        clients = tuple(clients)
        if len({client.sta_mac for client in clients}) != len(clients):
            raise ValueError("duplicate client ownership")
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        epoch = provenance(self.provenance_path, now_ns)
        bsses, _hops = inventory(raw)
        with self.lock:
            resets = self._observe_owners_locked(clients, bsses, epoch, now_ns, complete=True)
        raw["load_collection"] = {
            "schema": "easymesh.load.collection.v1", "sampled_monotonic_ns": now_ns,
            "epoch": epoch, "owner_resets": resets,
        }

    def _observe_owners_locked(self, clients, bsses, epoch, now_ns, *, complete=False):
        if epoch != self.epoch:
            self.epoch = epoch
            self.floor_ns = now_ns
            self.loads.clear()
            self.traffic.clear()
            self.contexts.clear()
            self.client_owners.clear()
        resets = []
        if epoch is None:
            return resets
        contexts = {bssid: (bss["device_id"].lower(), bss["radio_id"].lower(), bss.get("channel"))
                    for bssid, bss in bsses.items()
                    if bss.get("ssid") in {"private_ssid", "iot_ssid"} and bss.get("channel")
                    and bss.get("device_id") and bss.get("radio_id")}
        for bssid in set(self.contexts) - contexts.keys():
            self.contexts.pop(bssid)
        for bssid, identity in contexts.items():
            prior = self.contexts.get(bssid)
            if prior is None or prior[0] != identity:
                self.contexts[bssid] = (identity, now_ns)
        if complete:
            for station in set(self.client_owners) - {client.sta_mac for client in clients}:
                owner, _floor = self.client_owners.pop(station)
                self.traffic.pop((owner[0], station), None)
        for client in clients:
            owner = (client.connected_device_id, client.connected_bssid)
            context = self.contexts.get(client.connected_bssid)
            if context is None or context[0][0] != client.connected_device_id:
                self.client_owners.pop(client.sta_mac, None)
                continue
            previous_owner = self.client_owners.get(client.sta_mac)
            if previous_owner is None or previous_owner[0] != owner:
                cached = self.traffic.pop((client.connected_device_id, client.sta_mac), None)
                self.client_owners[client.sta_mac] = (owner, now_ns)
                resets.append({
                    "sta_mac": client.sta_mac, "previous_owner": previous_owner[0] if previous_owner else None,
                    "owner": owner, "floor_monotonic_ns": now_ns,
                    "discarded_report_monotonic_ns": cached["monotonic_ns"] if cached else None,
                    "discarded_report_received_at": cached["received_at"] if cached else None,
                })
        return resets

    def enrich(self, snapshot, raw, *, now_ns=None, observed_at=None):
        if observed_at is None:
            observed_at = format_time(datetime.now(timezone.utc)) if now_ns is None else snapshot.observed_at
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        snapshot = replace(snapshot, observed_at=observed_at)
        epoch = provenance(self.provenance_path, now_ns)
        bsses, hops = inventory(raw)
        loads, activity = [], []
        collection = raw.get("load_collection")
        if collection is None or "enriched_monotonic_ns" in collection or collection["epoch"] != epoch:
            collection = raw["load_collection"] = {
                "schema": "easymesh.load.collection.v1", "sampled_monotonic_ns": now_ns,
                "epoch": epoch, "owner_resets": [],
            }
        collection["enriched_monotonic_ns"] = now_ns
        with self.lock:
            collection["owner_resets"].extend(
                self._observe_owners_locked(snapshot.clients, bsses, epoch, now_ns))
            if epoch is None or self.error or (self.child is not None and self.child.poll() is not None):
                return replace(snapshot, schema_version=2, bss_loads=(), client_activity=())
            for bssid, (identity, floor) in self.contexts.items():
                row = self.loads.get((identity[0], bssid))
                if row is None or row["monotonic_ns"] < floor or not 0 <= now_ns - row["monotonic_ns"] <= 5000000000:
                    continue
                timestamp = format_time(datetime.fromtimestamp(row["received_at"], timezone.utc))
                loads.append(BssLoadObservation(bssid, identity[0], identity[1], identity[2],
                    row["utilization"], row["station_count"], timestamp, epoch, hops.get(identity[0]),
                    transport=row["transport"]))
            available = {row.bssid: row for row in loads}
            for client in snapshot.clients:
                owner = self.client_owners.get(client.sta_mac)
                row = self.traffic.get((client.connected_device_id, client.sta_mac))
                load = available.get(client.connected_bssid)
                if (owner is None or owner[0] != (client.connected_device_id, client.connected_bssid)
                        or load is None or row is None or row["rates"] is None
                        or row["transport"] != load.transport
                        or row["interval_started_monotonic_ns"] <= self.contexts[client.connected_bssid][1]
                        or row["interval_started_monotonic_ns"] <= owner[1]
                        or not 0 <= now_ns - row["monotonic_ns"] <= 5000000000):
                    continue
                timestamp = format_time(datetime.fromtimestamp(row["received_at"], timezone.utc))
                activity.append(ClientActivityObservation(client.sta_mac, client.connected_bssid,
                    row["rates"]["packets_per_second"], row["interval"], timestamp, epoch,
                    transport=row["transport"], bytes_per_second=row["rates"]["bytes_per_second"],
                    retries_per_second=row["rates"]["retries_per_second"],
                    errors_per_second=row["rates"]["errors_per_second"],
                    tx_errors_per_second=row["rates"]["tx_errors_per_second"],
                    rx_errors_per_second=row["rates"]["rx_errors_per_second"]))
        return replace(snapshot, schema_version=2, bss_loads=tuple(loads), client_activity=tuple(activity))

    def close(self):
        if self.child is not None:
            self.child.stdin.close()
            try:
                self.child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=3)
            for thread in self.threads:
                thread.join(timeout=3)
            self.child.stdout.close()
            self.child.stderr.close()
