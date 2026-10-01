from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import subprocess


def timestamp(value):
    normalized = re.sub(r"(\.\d{6})\d+(?=Z|[+-]|$)", r"\1", value)
    return dt.datetime.fromisoformat(normalized.replace("Z", "+00:00")).timestamp()


def statistics(values):
    values = sorted(value for value in values if value is not None)
    if not values:
        return {"count": 0}
    return {"count": len(values), "median": round(values[len(values) // 2], 3),
            "p95": round(values[min(len(values) - 1, int(len(values) * 0.95))], 3),
            "maximum": round(values[-1], 3)}


def journal_events(directory):
    index_path = directory / "journal-index.json"
    files = ["live-events.jsonl"]
    segments = {}
    if index_path.exists():
        index = json.loads(index_path.read_text())
        if not index["complete"] or index["retained_from_sequence"] != 1:
            raise ValueError("complete beginning-to-end evidence is required")
        files = [segment["file"] for segment in index["segments"]]
        segments = {segment["file"]: segment for segment in index["segments"]}
        if len(segments) != len(files):
            raise ValueError("duplicate journal segment")
    previous = None
    sequence = 0
    for name in files:
        if Path(name).name != name:
            raise ValueError("unsafe journal segment path")
        segment_bytes = 0
        with (directory / name).open() as stream:
            for line in stream:
                segment_bytes += len(line.encode())
                event = json.loads(line)
                unsigned = dict(event)
                claimed = unsigned.pop("event_hash")
                actual = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
                if actual != claimed or event["previous_event_hash"] != previous or event["sequence"] != sequence + 1:
                    raise ValueError(f"invalid event chain at {event['sequence']}")
                previous, sequence = claimed, event["sequence"]
                yield event
        segment = segments.get(name)
        if segment and (segment_bytes != segment["bytes"] or sequence != segment["last_sequence"] or previous != segment["last_event_hash"]):
            raise ValueError("journal segment is incomplete: " + name)
    storage_path = directory / "storage-summary.json"
    if storage_path.exists():
        storage = json.loads(storage_path.read_text())["journal"]
        if not storage["complete"] or storage["queued_bytes"] or storage["written_sequence"] != sequence:
            raise ValueError("journal shutdown was incomplete")


WIFI_FIELDS = ["frame.time_epoch", "wlan.sa", "wlan.da", "wlan.fixed.action_code",
               "wlan.fixed.dialog_token", "wlan.fixed.bss_transition_status_code",
               "wlan.fixed.bss_transition_target_bss", "wlan.fc.protected"]
CMDU_FIELDS = ["frame.time_epoch", "eth.src", "eth.dst", "ieee1905.message_type",
               "ieee1905.steering_req.source_bssid", "ieee1905.steering_req.target_mac",
               "ieee1905.steering_req.target_bssid", "ieee1905.btm_report.mac_addr",
               "ieee1905.btm_report.status", "ieee1905.btm_report.source_bssid",
               "ieee1905.btm_report.target_bssid", "ieee1905.assoc_event.client_mac",
               "ieee1905.assoc_event.agent_bssid", "ieee1905.assoc_event.assoc_event"]


def decode_packets(directory, name, fields, packet_filter):
    summary = json.loads((directory / "trace-summary.json").read_text())
    capture = summary["captures"].get(name)
    if not capture or not capture["history_complete"]:
        raise ValueError(name + " capture is absent, incomplete or has wrapped")
    packets = []
    for filename in capture["files"]:
        if Path(filename).name != filename:
            raise ValueError("unsafe capture path")
        command = ["tshark", "-n", "-r", str(directory / filename), "-Y", packet_filter,
                   "-T", "fields", "-E", "occurrence=f"]
        for field in fields:
            command.extend(["-e", field])
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        for line in result.stdout.splitlines():
            values = line.split("\t")
            if len(values) != len(fields):
                raise ValueError("unexpected tshark field count")
            packet = dict(zip(fields, values))
            packet["at"] = float(packet["frame.time_epoch"])
            packets.append(packet)
    return sorted(packets, key=lambda packet: packet["at"])


def matched_packet(packets, start, end, predicates):
    return next((packet for packet in packets if start <= packet["at"] <= end
                 and all(packet.get(field) == value for field, value in predicates.items())), None)


def analyze(room_directory, trace_directory, allow_incomplete=False):
    trace_summary = json.loads((trace_directory / "trace-summary.json").read_text())
    if not trace_summary["complete"] and not allow_incomplete:
        raise ValueError("trace qualification failed")
    trace_start = timestamp(trace_summary["started_at"])
    trace_end = timestamp(trace_summary["finished_at"])
    wifi = decode_packets(trace_directory, "wifi", WIFI_FIELDS, "wlan.fc.type_subtype == 0x0d")
    cmdu = decode_packets(trace_directory, "cmdu", CMDU_FIELDS, "ieee1905")
    inventory = json.loads((trace_directory / "inventory.json").read_text())
    mac_by_container = {node["name"]: node["state"]["network"]["wlan0"]["hwaddr"]
                        for node in inventory if node["name"].startswith("wlan-client")}
    client_connections = defaultdict(list)
    for event in journal_events(trace_directory):
        payload = event["payload"]
        line = payload.get("line", "")
        connected = re.search(r"CTRL-EVENT-CONNECTED - Connection to ([0-9a-f:]{17})", line)
        if connected:
            client_connections[mac_by_container[payload["container"]]].append(
                {"at": timestamp(event["recorded_at"]), "bssid": connected.group(1)})
    actions = []
    active = {}
    world = None
    network_changes = defaultdict(list)
    previous_owners = {}
    counts = Counter()
    for event in journal_events(room_directory):
        counts[event["kind"]] += 1
        payload = event["payload"]
        observed = timestamp(event["recorded_at"])
        if event["kind"] == "room.world.committed":
            for entry in active.values():
                entry["world_ended_at"] = observed
            world = payload["world"]["name"]
            active.clear()
        elif event["kind"] == "optimizer.action":
            station = payload["decision"]["sta_mac"]
            if payload["phase"] == "requested":
                if not trace_start <= observed <= trace_end:
                    continue
                entry = {"at": observed, "world": world, "station": station,
                         "source": payload["decision"]["source_bssid"], "target": payload["decision"]["target_bssid"],
                         "accepted": None, "verification": None}
                actions.append(entry)
                active[station] = entry
            elif station in active:
                active[station]["accepted"] = payload["result"]["success"]
                active[station]["dispatch_ms"] = (observed - active[station]["at"]) * 1000
        elif event["kind"] == "optimizer.verification":
            entry = active.get(payload["subject_mac"])
            if entry and entry["target"] == payload["target_bssid"]:
                entry["verification"] = payload["reason"]
        elif event["kind"] == "network.snapshot":
            owners = {client["sta_mac"]: client["connected_bssid"] for client in payload["clients"]}
            for station, owner in owners.items():
                if previous_owners.get(station) != owner:
                    network_changes[station].append({"at": observed, "bssid": owner})
            previous_owners = owners
    for index, action in enumerate(actions):
        end = min(action["at"] + 40, trace_end, action.get("world_ended_at", float("inf")),
                  next((other["at"] for other in actions[index + 1:] if other["station"] == action["station"]), float("inf")))
        action["observation_window_seconds"] = end - action["at"]
        request = matched_packet(wifi, action["at"], end,
            {"wlan.sa": action["source"], "wlan.da": action["station"], "wlan.fixed.action_code": "7"})
        response = matched_packet(wifi, request["at"], end,
            {"wlan.sa": action["station"], "wlan.da": action["source"], "wlan.fixed.action_code": "8",
             "wlan.fixed.dialog_token": request["wlan.fixed.dialog_token"]}) if request else None
        connection = next((entry for entry in client_connections[action["station"]]
                           if action["at"] <= entry["at"] <= end and entry["bssid"] == action["target"]), None)
        reported = next((entry for entry in network_changes[action["station"]]
                         if action["at"] <= entry["at"] <= end and entry["bssid"] == action["target"]), None)
        controller_request = matched_packet(cmdu, action["at"], end,
            {"ieee1905.steering_req.target_mac": action["station"],
             "ieee1905.steering_req.source_bssid": action["source"].replace(":", ""),
             "ieee1905.steering_req.target_bssid": action["target"].replace(":", "")})
        controller_report = matched_packet(cmdu, action["at"], end,
            {"ieee1905.btm_report.mac_addr": action["station"],
             "ieee1905.btm_report.source_bssid": action["source"].replace(":", "")})
        association_report = matched_packet(cmdu, action["at"], end,
            {"ieee1905.assoc_event.client_mac": action["station"],
             "ieee1905.assoc_event.agent_bssid": action["target"].replace(":", ""),
             "ieee1905.assoc_event.assoc_event": "1"})
        protected = matched_packet(wifi, action["at"], end,
            {"wlan.sa": action["source"], "wlan.da": action["station"], "wlan.fc.protected": "1"})
        action.update(cmdu_request_at=controller_request["at"] if controller_request else None,
            btm_request_at=request["at"] if request else None, btm_response_at=response["at"] if response else None,
            btm_status=response["wlan.fixed.bss_transition_status_code"] if response else None,
            btm_response_target=response["wlan.fixed.bss_transition_target_bss"] if response else None,
            cmdu_btm_report_at=controller_report["at"] if controller_report else None,
            cmdu_btm_report_status=controller_report["ieee1905.btm_report.status"] if controller_report else None,
            cmdu_associated_at=association_report["at"] if association_report else None,
            status_report_disagrees_with_clear_response=bool(controller_report and response and
                controller_report["ieee1905.btm_report.status"] != response["wlan.fixed.bss_transition_status_code"]),
            protected_action_at=protected["at"] if protected else None,
            client_connected_at=connection["at"] if connection else None,
            room_reported_at=reported["at"] if reported else None)
        if action["accepted"] is not True:
            classification = "dispatch_not_accepted"
        elif not request and protected:
            classification = "protected_action_payload_not_decodable"
        elif not request:
            classification = "no_matching_btm_tx_captured"
        elif not response:
            classification = "btm_tx_without_captured_response"
        elif action["btm_status"] != "0":
            classification = "client_rejected_btm"
        elif action["btm_response_target"] and action["btm_response_target"] != action["target"]:
            classification = "btm_response_names_different_target"
        elif not connection:
            classification = "accepted_btm_without_target_client_event"
        elif not reported:
            classification = "client_connected_without_target_native_readback"
        else:
            classification = "client_and_native_readback_converged"
        if classification not in {"dispatch_not_accepted", "client_and_native_readback_converged", "client_rejected_btm"} and end < action["at"] + 40:
            classification = "observation_window_ended:" + classification
        action["classification"] = classification
    def intervals(start, end):
        return statistics((entry[end] - entry[start]) * 1000 for entry in actions
                          if entry.get(start) is not None and entry.get(end) is not None and entry[end] >= entry[start])
    return {"schema": "easymesh.coordination-trace-report.v1", "qualified_trace": trace_summary["complete"],
            "trace_window": {"start": trace_start, "end": trace_end}, "event_counts": dict(counts),
            "trace_summary": trace_summary, "classifications": dict(Counter(entry["classification"] for entry in actions)),
            "target_outcomes": {
                "client_connection_observed": sum(entry["client_connected_at"] is not None for entry in actions),
                "native_owner_observed": sum(entry["room_reported_at"] is not None for entry in actions),
                "both_observed": sum(entry["client_connected_at"] is not None and entry["room_reported_at"] is not None for entry in actions),
                "native_btm_reports": sum(entry["cmdu_btm_report_at"] is not None for entry in actions),
                "native_btm_status_disagreements": sum(entry["status_report_disagrees_with_clear_response"] for entry in actions)},
            "latency_ms": {"request_to_cmdu": intervals("at", "cmdu_request_at"),
                "request_to_btm_tx": intervals("at", "btm_request_at"),
                "btm_request_to_response": intervals("btm_request_at", "btm_response_at"),
                "btm_request_to_client_event": intervals("btm_request_at", "client_connected_at"),
                "client_event_to_cmdu_association": intervals("client_connected_at", "cmdu_associated_at"),
                "cmdu_association_to_room_native_readback": intervals("cmdu_associated_at", "room_reported_at"),
                "client_event_to_room_native_readback": intervals("client_connected_at", "room_reported_at")},
            "limitations": ["TX monitor frames are not receiving-client delivery proof.",
                "Missing capture matches are boundaries for investigation, not proof of native stack failure.",
                "Client events use collector receipt time; packet captures use the VM kernel clock.",
                "Connections and readback are matched within the 40-second action window.",
                "A native readback observed before the client event is excluded from nonnegative delay statistics."],
            "actions": actions}


def main():
    parser = argparse.ArgumentParser(description="Correlate completed room journals, client events and tshark packet evidence.")
    parser.add_argument("--room", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-incomplete", action="store_true", help="Inspect positive evidence only; never marks the trace qualified.")
    arguments = parser.parse_args()
    report = analyze(arguments.room, arguments.trace, arguments.allow_incomplete)
    arguments.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("classifications", "latency_ms")}, indent=2))


if __name__ == "__main__":
    main()
