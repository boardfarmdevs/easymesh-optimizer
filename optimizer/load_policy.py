from __future__ import annotations

from dataclasses import replace
from .model import parse_time
from .policy import Evaluation, ThresholdPolicy, _fresh
from .state import PolicyState


class LoadAwarePolicy(ThresholdPolicy):
    def _load_selection(self, snapshot, client, candidates, now):
        if client.rcpi < self.config.load_minimum_target_rcpi:
            return None
        evidence = {"policy": "native-load-v1", "utilization_unit": "octet-0..255",
                    "capacity_estimate": False,
                    "maximum_report_age_seconds": self.config.load_maximum_age_seconds,
                    "maximum_report_skew_seconds": self.config.load_maximum_report_skew_seconds,
                    "required_hold_seconds": self.config.load_condition_hold_seconds,
                    "candidate_report_skew_seconds": {}}
        hold = lambda reason: {"target": None, "reason": reason, "evidence": evidence}
        loads = {row.bssid: row for row in snapshot.bss_loads
                 if row.source == "native_ap_metrics" and _fresh(
                     row.observed_at, now, self.config.load_maximum_age_seconds)}
        current = loads.get(client.connected_bssid)
        if current is None or current.device_id != client.connected_device_id or current.backhaul_hops is None:
            return hold("native_load_current_unavailable")
        evidence.update(current_utilization=current.utilization, current_radio=current.radio_id,
                        current_epoch=current.epoch, current_backhaul_hops=current.backhaul_hops,
                        current_observed_at=current.observed_at, current_transport=current.transport)
        if current.utilization < self.config.load_high_utilization:
            return hold("native_load_current_acceptable")
        activity = next((row for row in snapshot.client_activity if row.sta_mac == client.sta_mac
                       and row.bssid == client.connected_bssid and row.epoch == current.epoch
                       and row.source == "native_sta_traffic" and _fresh(
                           row.observed_at, now, self.config.load_maximum_age_seconds)), None)
        if activity is None:
            return hold("native_load_activity_unavailable")
        evidence.update(client_packets_per_second=activity.packets_per_second, activity_interval_seconds=activity.interval_seconds)
        if activity.packets_per_second < self.config.load_minimum_activity_packets_per_second:
            return hold("native_load_client_idle")
        choices = []
        for candidate in candidates:
            target = loads.get(candidate.bssid)
            if target is not None:
                evidence["candidate_report_skew_seconds"][candidate.bssid] = abs((
                    parse_time(target.observed_at) - parse_time(current.observed_at)).total_seconds())
            if (target is None or target.epoch != current.epoch or target.device_id != candidate.device_id
                    or target.radio_id == current.radio_id or target.backhaul_hops is None
                    or target.channel == current.channel
                    or abs((parse_time(target.observed_at) - parse_time(current.observed_at)).total_seconds())
                    > self.config.load_maximum_report_skew_seconds
                    or target.backhaul_hops > current.backhaul_hops or candidate.band != client.band
                    or candidate.rcpi < self.config.load_minimum_target_rcpi
                    or client.rcpi - candidate.rcpi > self.config.load_maximum_signal_loss_rcpi
                    or target.utilization > self.config.load_maximum_target_utilization
                    or current.utilization - target.utilization < self.config.load_minimum_advantage):
                continue
            choices.append((target, candidate))
        if not choices:
            return hold("native_load_no_safe_quieter_target")
        target, candidate = min(choices, key=lambda row: (
            row[0].utilization, row[0].backhaul_hops, -row[1].rcpi, row[1].bssid))
        evidence.update(target_utilization=target.utilization, target_radio=target.radio_id,
                        target_backhaul_hops=target.backhaul_hops, target_epoch=target.epoch,
                        target_observed_at=target.observed_at, target_transport=target.transport)
        return {"target": candidate, "reason": "native_load_margin", "evidence": evidence}

    def evaluate(self, snapshot, prior=None):
        prior = prior or PolicyState()
        result = super().evaluate(snapshot, prior)
        balancing = sorted((item for item in result.decisions if item.action == "steer"
                            and item.reason == "native_load_margin_hold_satisfied"),
                           key=lambda item: (-item.load_evidence["client_packets_per_second"], item.sta_mac))
        if not balancing:
            return result
        now = parse_time(snapshot.observed_at)
        settling = any(item.phase == "pending" or (item.last_action_at is not None and
                       (now - parse_time(item.last_action_at)).total_seconds() < self.config.load_settle_seconds)
                       for item in prior.clients)
        settling = settling or any(item.action == "steer" and item.reason != "native_load_margin_hold_satisfied"
                                   for item in result.decisions)
        allowed = None if settling else balancing[0].sta_mac
        decisions = []
        state = result.state
        for decision in result.decisions:
            if decision in balancing and decision.sta_mac != allowed:
                decision = replace(decision, action="none", reason="native_load_settling" if settling else "native_load_batch_deferred")
                state = state.replace(prior.for_sta(decision.sta_mac))
            decisions.append(decision)
        return Evaluation(result.policy_hash, tuple(decisions), state)


def policy_for(config):
    return LoadAwarePolicy(config) if config.load_aware_enabled else ThresholdPolicy(config)
