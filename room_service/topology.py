from __future__ import annotations

from typing import Any
from datetime import datetime, timezone

from optimizer.model import parse_time


def project_topology(topology: dict[str, Any] | None, roles_by_bssid: dict[str, str], *, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    result = {
        "source": "prplmesh_nbapi",
        "available": topology is not None,
        "nodes": [],
        "backhaul_edges": [],
        "wired_edges": [],
        "unresolved_edges": 0,
    }
    if topology is None:
        return result
    roles_by_device = {}
    radios_by_bssid = {
        str(bss.get("bssid", "")).lower(): radio
        for device in topology.get("devices", [])
        for radio in device.get("radios", [])
        for bss in radio.get("bsses", [])
    }
    for device in topology.get("devices", []):
        role = next((
            roles_by_bssid[bss["bssid"].lower()]
            for radio in device.get("radios", [])
            for bss in radio.get("bsses", [])
            if bss.get("bssid", "").lower() in roles_by_bssid
        ), None)
        if role is None:
            continue
        device_id = str(device["id"]).lower()
        roles_by_device[device_id] = role
        backhaul = device.get("backhaul") or {}
        result["nodes"].append({
            "role": role,
            "device_id": device_id,
            "name": "Agent-1" if role == "gateway" else "Extender-" + role.rsplit("_", 1)[1],
            "source_name": device.get("name", ""),
            "backhaul_media": backhaul.get("type") or "",
            "upstream_bssid": str(backhaul.get("mac") or "").lower()
            if str(backhaul.get("mac") or "").lower() in roles_by_bssid else "",
        })
    for device in topology.get("devices", []):
        backhaul = device.get("backhaul") or {}
        parent_id = str(backhaul.get("parent_id") or "").lower()
        if not parent_id or parent_id == "00:00:00:00:00:00":
            continue
        parent = roles_by_device.get(parent_id)
        child = roles_by_device.get(str(device["id"]).lower())
        if parent is None or child is None:
            result["unresolved_edges"] += 1
            continue
        if str(backhaul.get("type") or "").lower() not in ("wi-fi", "wifi", "wireless"):
            # an Agent on a wired backhaul: an Ethernet child of its parent, never a Wi-Fi edge
            result["wired_edges"].append({"parent_role": parent, "child_role": child,
                                          "media_type": backhaul.get("type") or "unknown"})
            continue
        upstream = str(backhaul.get("mac") or "").lower()
        if roles_by_bssid.get(upstream) != parent:
            upstream = ""
        radio = radios_by_bssid.get(upstream, {})
        band = str(radio.get("band") or "").replace(" GHz", "") or None
        signal = {}
        if upstream and str(backhaul.get("type") or "").lower() == "wi-fi":
            timestamp = backhaul.get("signal_updated_at")
            rssi = backhaul.get("signal_dbm")
            status = "invalid"
            try:
                age = (now - parse_time(timestamp)).total_seconds()
                if type(rssi) in (int, float) and -110 <= rssi <= 0 and age >= 0:
                    status = "fresh" if age <= 5 else "stale"
            except (ValueError, TypeError, AttributeError, OverflowError):
                pass
            signal = {"status": status, "observed_at": timestamp,
                      "rssi_dbm": rssi if status == "fresh" else None,
                      "source": "prplmesh_nbapi_backhaul_stats"}
        result["backhaul_edges"].append({
            "parent_role": parent,
            "child_role": child,
            "media_type": backhaul.get("type") or "unknown",
            "band": band,
            "channel": radio.get("channel"),
            "upstream_bssid": upstream,
            "backhaul_sta": str(backhaul.get("station_mac") or "").lower(),
            "signal": signal,
        })
    result["nodes"].sort(key=lambda item: item["role"])
    result["backhaul_edges"].sort(key=lambda item: (item["parent_role"], item["child_role"]))
    result["wired_edges"].sort(key=lambda item: (item["parent_role"], item["child_role"]))
    return result
