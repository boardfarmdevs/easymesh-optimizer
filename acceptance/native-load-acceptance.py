import argparse
import json
import os
from pathlib import Path
import time

from optimizer.load_observer import NativeLoadProvider, inventory
from optimizer.observer import ControllerObserver


def main():
    parser = argparse.ArgumentParser(description="Bounded read-only native AP load coverage qualification")
    parser.add_argument("--stack", choices=("rdk", "prpl"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=20)
    args = parser.parse_args()
    if os.geteuid() or not 10 <= args.seconds <= 60:
        parser.error("requires root in the lab VM and 10..60 seconds")
    args.output.mkdir(parents=True, exist_ok=False)
    if args.stack == "prpl":
        from optimizer.prplmesh import PrplMeshObserver
        observer = PrplMeshObserver()
    else:
        observer = ControllerObserver()
    controller = "prpl-controller" if args.stack == "prpl" else "bpibroadband"
    report = {"stack": args.stack, "state": "failed", "read_only": True,
        "samples": 0, "first_complete_seconds": None, "transport_owners": {},
        "native_report_timestamps": {}, "receiver_stopped": False}
    provider = None
    snapshot = None
    try:
        provider = NativeLoadProvider(controller, byte_counter_unit_bytes=1024 if args.stack == "prpl" else 1)
        observer.ownership_observer = provider.observe_owners
        started = time.monotonic()
        with (args.output / "samples.jsonl").open("w") as stream:
            while time.monotonic() - started < args.seconds:
                snapshot = observer.observe()
                snapshot = provider.enrich(snapshot, observer.last_raw)
                if len(snapshot.clients) != 20:
                    raise RuntimeError("requires the default 20-client lab")
                bsses, _hops = inventory(observer.last_raw)
                expected = {bssid for bssid, row in bsses.items()
                    if row.get("ssid") in {"private_ssid", "iot_ssid"} and row.get("channel")}
                report["missing_bsses"] = sorted(expected - {row.bssid for row in snapshot.bss_loads})
                report["missing_activity"] = sorted({row.sta_mac for row in snapshot.clients}
                    - {row.sta_mac for row in snapshot.client_activity})
                report["station_count_mismatches"] = [
                    {"bssid": row.bssid, "reported": row.station_count,
                        "associated_clients": sum(client.connected_bssid == row.bssid for client in snapshot.clients)}
                    for row in snapshot.bss_loads
                    if row.station_count != sum(client.connected_bssid == row.bssid for client in snapshot.clients)]
                report["samples"] += 1
                for row in snapshot.bss_loads:
                    owners = report["transport_owners"].setdefault(row.transport, [])
                    if row.device_id not in owners:
                        owners.append(row.device_id)
                    timestamps = report["native_report_timestamps"].setdefault(row.bssid, [])
                    if row.observed_at not in timestamps:
                        timestamps.append(row.observed_at)
                stream.write(json.dumps(snapshot.to_dict()) + "\n")
                stream.flush()
                if (expected and not report["missing_bsses"] and not report["missing_activity"]
                        and not report["station_count_mismatches"]
                        and report["first_complete_seconds"] is None):
                    report["first_complete_seconds"] = time.monotonic() - started
                if provider.error:
                    raise RuntimeError(provider.error)
                time.sleep(.5)
        if report["first_complete_seconds"] is None:
            raise RuntimeError("native AP load or client activity coverage incomplete")
        if any(len(values) < 2 for values in report["native_report_timestamps"].values()):
            raise RuntimeError("native report timestamps did not advance")
        if args.stack == "prpl":
            transports = report["transport_owners"]
            if len(transports.get("prpl-local-broker", [])) != 1 or len(transports.get("prpl-1905-broker", [])) != 4:
                raise RuntimeError("expected one colocated and four remote native report sources")
        report["state"] = "passed"
    except Exception as error:
        report["error"] = str(error)
    finally:
        if provider is not None:
            provider.close()
            report["receiver_stopped"] = provider.child.poll() is not None
            if snapshot is not None:
                closed = provider.enrich(snapshot, observer.last_raw)
                report["closed_receiver_unavailable"] = not closed.bss_loads and not closed.client_activity
                if not report["closed_receiver_unavailable"]:
                    report["state"] = "failed"
            if not report["receiver_stopped"]:
                report["state"] = "failed"
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "native_report_timestamps"}))
    return 0 if report["state"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
