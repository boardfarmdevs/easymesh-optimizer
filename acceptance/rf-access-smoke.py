#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from urllib.request import urlopen


def validate(catalog, inspection, layout, *, now=None, require_backhaul_load=False):
    now = now or datetime.now(timezone.utc).timestamp()
    errors = []
    if catalog.get("schema") != "easymesh.rf-properties.v1":
        errors.append("missing shared property catalog")
    if inspection.get("schema") != "easymesh.rf-inspection.v2":
        errors.append("missing shared inspection v2")
    if inspection.get("enabled") is not True or inspection.get("error"):
        errors.append("native RF receiver unavailable: " + str(inspection.get("error")))
    envelope = inspection.get("observations") or {}
    if envelope.get("schema") != "easymesh.rf-observations.v1" or envelope.get("decision_inputs") is not False:
        errors.append("missing observation-only envelope")
    valid = []
    for record in envelope.get("records", []):
        if record.get("state") != "valid":
            if record.get("value") is not None:
                errors.append("unavailable record exposes a value")
            continue
        try:
            age = now - datetime.fromisoformat(record["observed_at"].replace("Z", "+00:00")).timestamp()
            if not 0 <= age <= record["maximum_age_seconds"] + 1:
                errors.append("valid record exceeded freshness budget")
        except (ValueError, TypeError, KeyError, AttributeError):
            errors.append("valid record lacks freshness identity")
        if record.get("property") == "native_utilization":
            if type(record.get("value")) is not int or not 0 <= record["value"] <= 255:
                errors.append("invalid utilization byte")
            else:
                valid.append(record)
    if not valid:
        errors.append("no fresh native utilization record")
    if layout.get("schema") != "easymesh.room-layout.v1" or not layout.get("rf_observations", {}).get("enabled"):
        errors.append("topology RF projection unavailable")
    backhaul = inspection.get("backhaul") or {}
    if backhaul.get("schema") != "easymesh.backhaul-observations.v1" or backhaul.get("decision_inputs") is not False:
        errors.append("backhaul explanation unavailable")
    if require_backhaul_load:
        paths = backhaul.get("paths", [])
        if not paths:
            errors.append("no native backhaul paths")
        for path in paths:
            if path.get("state") != "valid" or not path.get("links"):
                errors.append(str(path.get("role")) + ": native backhaul path unavailable")
            for link in path.get("links", []):
                if link.get("wireless") is False:
                    continue
                load = link.get("utilization") or {}
                identity = load.get("identity") or {}
                if (link.get("wireless") is not True or not link.get("frequency_mhz")
                        or not link.get("bssid") or load.get("state") != "valid"
                        or load.get("source") != "native_ap_metrics"
                        or identity.get("frequency_mhz") != link["frequency_mhz"]
                        or identity.get("bssid") != link["bssid"]
                        or type(load.get("value")) is not int or not 0 <= load["value"] <= 255):
                    errors.append(str(link.get("role")) + ": fresh exact-context backhaul load unavailable")
                try:
                    age = now - datetime.fromisoformat(load["observed_at"].replace("Z", "+00:00")).timestamp()
                    if not 0 <= age <= load["maximum_age_seconds"] + 1:
                        errors.append("backhaul load exceeded freshness budget")
                except (ValueError, TypeError, KeyError, AttributeError):
                    errors.append("backhaul load lacks freshness identity")
    return errors


def main():
    parser = argparse.ArgumentParser(description="Bounded GET-only shared RF access check; no scans, steering or room changes.")
    parser.add_argument("--room-url", required=True)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--require-backhaul-load", action="store_true",
                        help="also require fresh native load on every wireless hop of each reported path")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.samples <= 10:
        parser.error("--samples must be between 1 and 10")
    if args.output.exists():
        parser.error("use a new output file to preserve evidence")
    observations = []
    for index in range(args.samples):
        sample = {"sample": index, "requests": {}}
        try:
            for name, suffix in (("catalog", "rf-catalog"), ("inspection", "rf-observations"), ("layout", "mesh-layout")):
                started = time.monotonic()
                with urlopen(args.room_url.rstrip("/") + "/api/demo/" + suffix, timeout=3) as response:
                    sample[name] = json.load(response)
                sample["requests"][name] = round(time.monotonic() - started, 4)
            sample["errors"] = validate(sample["catalog"], sample["inspection"], sample["layout"],
                                        require_backhaul_load=args.require_backhaul_load)
        except (OSError, ValueError) as error:
            sample["errors"] = [str(error)]
        observations.append(sample)
        print(json.dumps({"sample": index, "errors": sample["errors"], "request_seconds": sample["requests"]}), flush=True)
        if index + 1 < args.samples:
            time.sleep(1)
    passed = all(not sample["errors"] for sample in observations)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"passed": passed, "scope": "GET-only access; not room/physics qualification",
                                      "samples": observations}, indent=2) + "\n")
    return int(not passed)


if __name__ == "__main__":
    raise SystemExit(main())
