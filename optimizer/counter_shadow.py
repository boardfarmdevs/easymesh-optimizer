from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from .counter_guard import evaluate_counter_guard
from .load_observer import traffic_rates
from .model import ClientActivityObservation, format_time


def shadow_counter_trials(report, config):
    result = {"scope": "native_counter_veto_only", "actuation": False,
              "load_target_selection_qualified": False, "passed": False, "trials": []}
    if not config.load_counter_guard_enabled:
        raise ValueError("shadow qualification requires the explicit counter guard")
    if not report.get("medium_instance") or report.get("state") != "passed":
        return {**result, "error": "qualified native trials and actual medium instance required"}
    station, bssid = report["station"], report["bssid"]
    expected_owner = [bssid, report["frequency_mhz"]]
    for trial in report["trials"]:
        output = {"name": trial["name"], "decisions": [], "unavailable_windows": []}
        result["trials"].append(output)
        if trial.get("association_before") != expected_owner or trial.get("association_after") != expected_owner:
            output["unavailable_windows"].append("native_owner_unverified")
            continue
        before, after = trial["native_before"]["received_at"], trial["native_after"]["received_at"]
        rows = [row for row in report["native_reports"] if before <= row["received_at"] <= after
                and row["source"] == trial["native_before"]["source"]
                and any(load["bssid"] == bssid for load in row["loads"])
                and any(item["sta_mac"] == station for item in row["traffic"])]
        for previous, current in zip(rows, rows[1:]):
            interval = (current["monotonic_ns"] - previous["monotonic_ns"]) / 1e9
            if current["transport"] != previous["transport"] or current["received_at"] <= previous["received_at"]:
                output["unavailable_windows"].append("report_context_changed")
                continue
            counters = lambda row: next(item for item in row["traffic"] if item["sta_mac"] == station)
            rates = traffic_rates(counters(current), counters(previous), interval,
                                  byte_counter_unit_bytes=report["source_byte_unit"])
            if rates is None:
                output["unavailable_windows"].append("counter_reset_or_unbounded_interval")
                continue
            now = datetime.fromtimestamp(current["received_at"], timezone.utc)
            observed_at = format_time(now)
            activity = ClientActivityObservation(station, bssid, rates["packets_per_second"], interval,
                observed_at, report["medium_instance"], transport=current["transport"],
                **{name: rates[name] for name in ("bytes_per_second", "retries_per_second",
                                                "tx_errors_per_second", "rx_errors_per_second")})
            load = SimpleNamespace(bssid=bssid, source="native_ap_metrics", observed_at=observed_at,
                                   epoch=report["medium_instance"], transport=current["transport"])
            decision = evaluate_counter_guard(activity, load, station=station, bssid=bssid, now=now, config=config)
            output["decisions"].append({**decision, "previous_message_id": previous["message_id"],
                "message_id": current["message_id"], "source": current["source"],
                "replay_at_report_receipt": True, "counter_deltas": rates["deltas"]})
    states = {row["name"]: {item["state"] for item in row["decisions"]} for row in result["trials"]}
    result["passed"] = (states.get("baseline") == {"clear"} and states.get("recovery") == {"clear"}
                        and any("pressure" in states.get(name, set()) for name in ("data_loss", "ack_loss")))
    return result
