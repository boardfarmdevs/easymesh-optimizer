from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import math

from .model import format_time, normalize_band, parse_time


SCHEMA = "easymesh.rf-observations.v1"
CATALOG_SCHEMA = "easymesh.rf-properties.v1"
PROPERTIES = (
    ("rcpi", "Received channel power", "RCPI", "client-link", "native_observation", "signal"),
    ("configured_snr", "Configured directed SNR", "dB", "directed-link", "scenario", "diagnostic"),
    ("received_signal", "Received signal", "dBm", "directed-link", "model_observation", "diagnostic"),
    ("noise_reference", "Configured noise reference", "dBm", "medium", "model_parameter", "diagnostic"),
    ("cca_threshold", "Configured CCA threshold", "dBm", "medium", "model_parameter", "diagnostic"),
    ("noise", "Independent observed noise", "dBm", "radio-context", "native_observation", "unsupported"),
    ("native_utilization", "AP-reported utilization", "uint8/255", "bss", "native_observation", "load_opt_in"),
    ("station_count", "Associated stations", "stations", "bss", "native_observation", "inspection"),
    ("beacon_utilization", "Received beacon utilization", "uint8/255", "received-bss", "native_observation", "inspection"),
    ("modeled_busy", "Modeled channel busy", "percent", "radio-context", "model_observation", "diagnostic"),
    ("packets_per_second", "Native packet activity", "packets/s", "client-link", "native_observation", "load_opt_in"),
    ("bytes_per_second", "Native byte activity", "bytes/s", "client-link", "native_observation", "inspection"),
    ("retries_per_second", "AP TX retries", "retries/s", "client-link", "native_observation", "inspection"),
    ("tx_errors_per_second", "AP TX failures", "failures/s", "client-link", "native_observation", "inspection"),
    ("rx_errors_per_second", "AP RX drops", "drops/s", "client-link", "native_observation", "inspection"),
    ("backhaul_hops", "Wireless backhaul hops", "hops", "device", "native_observation", "load_opt_in"),
    ("receive_context", "Native receive context", "MHz", "radio-context", "native_observation", "inspection"),
    ("room_presence", "Room presence", "boolean", "radio", "scenario", "eligibility"),
    ("frequency", "Operating frequency", "MHz", "radio-context", "native_observation", "eligibility"),
    ("channel_width", "Native configured width", "MHz", "radio-context", "native_observation", "inspection"),
    ("packet_types", "Selected packet types", "frames", "directed-link", "model_observation", "diagnostic"),
    ("frame_loss", "Modeled frame loss", "frames", "directed-link", "model_observation", "diagnostic"),
    ("queue_delay", "Medium queue delay", "us", "medium", "diagnostic", "diagnostic"),
    ("active_rf_modes", "Enabled RF model modes", "profile", "medium", "model_parameter", "diagnostic"),
    ("physical_capacity", "Calibrated PHY capacity", "Mbit/s", "radio-context", "native_observation", "unsupported"),
)
DEFINITIONS = {row[0]: dict(zip(("id", "label", "unit", "scope", "kind", "usage"), row)) for row in PROPERTIES}


def property_catalog():
    properties = []
    for definition in DEFINITIONS.values():
        row = dict(definition)
        native = row["id"] in {"native_utilization", "station_count", "backhaul_hops"} or row["id"].endswith("_per_second")
        row.update(availability="unsupported" if row["usage"] == "unsupported" else "requires_fresh_evidence",
                   activation="Native report receiver; load policy is a separate opt-in" if native else
                   "Inspect active RF capabilities and the source view; catalog membership does not enable a mode",
                   observation_endpoint="/api/demo/rf-observations" if native else None,
                   maximum_age_seconds=5 if native else None,
                   source_view="Native AP / client metrics" if native else
                   "Decision evidence" if row["id"] == "rcpi" else "Room configured RF / Console NG diagnostics")
        properties.append(row)
    return {"schema": CATALOG_SCHEMA, "properties": properties,
            "states": ["valid", "unsupported", "missing", "stale", "invalid"],
            "usage_note": "Available fields are not necessarily used. Decision evidence preserves actual policy inputs.",
            "native_window_note": "Native report receipt time is not a known measurement window."}


def frequency_for(band, channel):
    try:
        band = normalize_band(band)
    except (ValueError, TypeError):
        return None
    if type(channel) is not int or channel <= 0:
        return None
    if band == "2.4" and channel <= 14:
        return 2484 if channel == 14 else 2407 + 5 * channel
    if band == "5" and channel <= 196:
        return 5000 + 5 * channel
    if band == "6" and channel <= 233:
        return 5935 if channel == 2 else 5950 + 5 * channel
    return None


def field_record(property_id, value, identity, observed_at, source, now, *, maximum_age=5,
                 window_seconds=None, reason="", supported=True):
    definition = DEFINITIONS[property_id]
    state = "missing"
    age = None
    if not supported or definition["usage"] == "unsupported":
        state = "unsupported"
    elif reason:
        state = "invalid"
    elif value is not None:
        try:
            age = (now - parse_time(observed_at)).total_seconds()
            valid_number = type(value) in (int, float) and math.isfinite(value)
            valid_range = (0 <= value <= 255 if property_id in {"native_utilization", "beacon_utilization"}
                           else 0 <= value <= 220 if property_id == "rcpi"
                           else 0 <= value <= 65535 if property_id == "station_count"
                           else True if definition["unit"] in {"dB", "dBm"}
                           else value >= 0) if valid_number else False
            if property_id in {"native_utilization", "beacon_utilization", "rcpi", "station_count"}:
                valid_range = valid_range and type(value) is int
            state = "invalid" if not valid_range or age < 0 else "stale" if age > maximum_age else "valid"
        except (ValueError, TypeError, AttributeError, OverflowError):
            state = "invalid"
    return {"property": property_id, "label": definition["label"], "unit": definition["unit"],
            "kind": definition["kind"], "scope": definition["scope"], "usage": definition["usage"],
            "state": state, "value": value if state == "valid" else None,
            "identity": dict(identity), "observed_at": observed_at, "age_seconds": age,
            "maximum_age_seconds": maximum_age, "window_seconds": window_seconds,
            "source": source, "reason": reason or ("" if state == "valid" else state)}


def observation_envelope(loads=(), activity=(), *, inventory=None, now=None, error=None):
    now = now or datetime.now(timezone.utc)
    inventory = inventory or {}
    records = []
    for row in loads:
        entry = asdict(row) if not isinstance(row, dict) else row
        context = inventory.get(entry["bssid"], {}) if entry.get("context_state") != "unverified" else {}
        identity = {key: entry.get(key) for key in ("bssid", "device_id", "radio_id", "channel", "epoch")}
        identity.update(band=context.get("band"), frequency_mhz=frequency_for(context.get("band"), entry["channel"]),
                        width_mhz=context.get("width_mhz"), context_state=entry.get("context_state", "verified"))
        for property_id, name in (("native_utilization", "utilization"), ("station_count", "station_count")):
            records.append({**field_record(property_id, entry.get(name), identity, entry.get("observed_at"),
                                          entry.get("source"), now, reason=error or ""),
                            "transport": entry.get("transport")})
    for row in activity:
        entry = asdict(row) if not isinstance(row, dict) else row
        identity = {key: entry.get(key) for key in ("sta_mac", "bssid", "epoch")}
        for property_id in ("packets_per_second", "bytes_per_second", "retries_per_second",
                            "tx_errors_per_second", "rx_errors_per_second"):
            records.append({**field_record(property_id, entry.get(property_id), identity, entry.get("observed_at"),
                                          entry.get("source"), now, window_seconds=entry.get("interval_seconds"),
                                          reason=error or ""), "transport": entry.get("transport")})
    return {"schema": SCHEMA, "published_at": format_time(now), "catalog_schema": CATALOG_SCHEMA,
            "records": records[:2048], "truncated": len(records) > 2048, "error": error, "decision_inputs": False,
            "sample_window_note": "Native load window unknown; timestamps identify report receipt."}
