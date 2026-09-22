from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from .model import parse_time
from .rf_observations import field_record, frequency_for


def backhaul_observations(network, inspection, *, now=None):
    now = now or datetime.now(timezone.utc)
    timestamp = network.get("observed_at")
    try:
        age = (now - parse_time(timestamp)).total_seconds()
        state = "valid" if 0 <= age <= 5 else "stale" if age > 5 else "invalid"
    except (ValueError, TypeError, AttributeError):
        state = "missing"
    mesh = network.get("mesh") or {}
    if mesh.get("available") is not True:
        state = "missing"
    nodes = {node["role"]: node for node in mesh.get("nodes", []) if node.get("role")}
    edges = {}
    ambiguous = set()
    for edge in mesh.get("backhaul_edges", []):
        child = edge.get("child_role")
        if child in edges:
            ambiguous.add(child)
        edges[child] = edge
    rows = []
    for role, node in sorted(nodes.items()):
        if role == "gateway":
            continue
        path, links, current = [], [], role
        reason = ""
        if state == "valid":
            while current != "gateway":
                if current in path:
                    reason = "cycle in native parent graph"
                    break
                path.append(current)
                edge = edges.get(current)
                if current in ambiguous or not edge or edge.get("parent_role") not in nodes:
                    reason = "native parent missing or ambiguous"
                    break
                parent = edge["parent_role"]
                media = str(edge.get("media_type") or "").lower()
                wireless = True if media in {"wireless lan", "wireless", "wifi", "wi-fi"} else False if media == "ethernet" else None
                identity = {"role": current, "parent_role": parent,
                            "bssid": edge.get("upstream_bssid"),
                            "frequency_mhz": frequency_for(edge.get("band"), edge.get("channel"))}
                signal = edge.get("signal") or {}
                signal_time = signal.get("observed_at") or signal.get("metric_observed_at")
                rssi = field_record("received_signal", signal.get("rssi_dbm"), identity,
                                    signal_time, "native_backhaul_report", now,
                                    reason="" if signal.get("status") == "fresh" else "signal not fresh")
                rssi["kind"] = "native_observation"
                loads = [row for row in inspection.get("bss_loads", [])
                         if identity["bssid"] and row.get("bssid") == identity["bssid"]
                         and row.get("device_id") == nodes[parent].get("device_id")
                         and row.get("source") == "native_ap_metrics"
                         and row.get("context_state") == "verified"
                         and row.get("frequency_mhz") == identity["frequency_mhz"]
                         and identity["frequency_mhz"] is not None]
                load = loads[0] if len(loads) == 1 else {}
                utilization = field_record("native_utilization", load.get("utilization"), identity,
                                           load.get("observed_at"), load.get("source"), now,
                                           reason=inspection.get("error") or "")
                traffic = field_record("bytes_per_second", None, identity, None, None, now)
                traffic["scope"] = "backhaul-link"
                traffic["reason"] = "no independently qualified backhaul byte-counter window"
                links.append({**identity, "media_type": media or "unknown", "wireless": wireless,
                              "signal": rssi, "utilization": utilization,
                              "traffic": traffic})
                current = parent
            if not reason:
                path.append("gateway")
        path_state = "invalid" if reason else state
        frequencies = Counter(link["frequency_mhz"] for link in links if link["frequency_mhz"] is not None)
        rows.append({"role": role, "device_id": node.get("device_id"), "state": path_state,
                     "reason": reason or ("" if path_state == "valid" else "native topology " + state),
                     "observed_at": timestamp, "maximum_age_seconds": 5,
                     "parent_role": path[1] if path_state == "valid" else None,
                     "path": path if path_state == "valid" else [],
                     "wireless_hops": sum(link["wireless"] for link in links)
                     if path_state == "valid" and all(link["wireless"] is not None for link in links) else None,
                     "links": links if path_state == "valid" else [],
                     "shared_frequency_hops": {str(frequency): count for frequency, count in frequencies.items()
                                               if count > 1} if path_state == "valid" else {}})
    return {"schema": "easymesh.backhaul-observations.v1", "observed_at": timestamp,
            "source": mesh.get("source"), "decision_inputs": False, "paths": rows,
            "note": "Native parent paths only. Shared-frequency hops indicate possible contention, not additive load or capacity. Traffic needs a qualified backhaul counter window."}
