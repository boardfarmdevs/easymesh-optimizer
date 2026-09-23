from __future__ import annotations

from .model import parse_time


def evaluate_counter_guard(activity, current, *, station, bssid, now, config):
    evidence = {"counter_guard_enabled": config.load_counter_guard_enabled,
                "scope": "native_counter_veto_only", "capacity_estimate": False}

    def result(reason, state="unavailable"):
        return {"reason": reason, "state": state, "evidence": evidence}

    def fresh(row):
        return row is not None and 0 <= (now - parse_time(row.observed_at)).total_seconds() <= config.load_maximum_age_seconds

    if not config.load_counter_guard_enabled:
        return result("native_counter_guard_disabled", "disabled")
    if (not fresh(current) or current.source != "native_ap_metrics" or current.bssid != bssid
            or not current.epoch):
        return result("native_load_current_unavailable")
    if (not fresh(activity) or activity.source != "native_sta_traffic" or activity.sta_mac != station
            or activity.bssid != bssid or activity.epoch != current.epoch):
        return result("native_load_activity_unavailable")
    evidence.update(activity_observed_at=activity.observed_at, activity_transport=activity.transport,
                    activity_interval_seconds=activity.interval_seconds, client_packets_per_second=activity.packets_per_second,
                    client_bytes_per_second=activity.bytes_per_second, bssid=bssid, sta_mac=station,
                    epoch=current.epoch, current_observed_at=current.observed_at)
    skew = abs((parse_time(activity.observed_at) - parse_time(current.observed_at)).total_seconds())
    evidence["activity_report_skew_seconds"] = skew
    if activity.transport != current.transport or skew > config.load_maximum_report_skew_seconds:
        return result("native_load_activity_context_mismatch")
    checks = []
    for name in ("retries_per_second", "tx_errors_per_second", "rx_errors_per_second"):
        value = getattr(activity, name)
        maximum = getattr(config, "load_maximum_" + name)
        checks.append({"property": name, "value": value, "maximum": maximum,
                       "state": "missing" if value is None else "exceeded" if value > maximum else "within_limit"})
    evidence["counter_checks"] = checks
    if any(check["state"] == "missing" for check in checks):
        return result("native_load_counter_evidence_unavailable")
    if any(check["state"] == "exceeded" for check in checks):
        return result("native_load_counter_pressure", "pressure")
    return result("native_load_counter_clear", "clear")
