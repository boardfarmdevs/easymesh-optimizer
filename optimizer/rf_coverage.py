from __future__ import annotations


DEMONSTRATIONS = {
    "rcpi": (("received-same-band-roam", "home-a-border-hover"), "signal_policy", "Fresh eligible gain, hold and native association; missing/stale signal abstains."),
    "configured_snr": (("rf-asymmetric-ack",), "directed_readback", "Read back both directions and exact frequency in one generation; never rank from room truth."),
    "received_signal": (("rf-asymmetric-ack", "received-same-band-roam"), "received_evidence", "Preserve direction/source/time; a last packet is not a fresh native candidate."),
    "noise_reference": (("home-a-stationary",), "model_profile", "Observe compiled -91 dBm reference; no noise or power action."),
    "cca_threshold": (("received-discovery-recovery",), "model_profile", "Observe compiled -90 dBm threshold and separate CCA drops; no threshold control."),
    "noise": (("home-a-stationary",), "unsupported", "Independent noise remains unsupported, never zero or inferred from signal minus SNR."),
    "native_utilization": (("traffic-low-high-off", "traffic-quieter-ap"), "load_policy", "Fresh native octets only; same-channel targets excluded; no overload means no balancing."),
    "station_count": (("home-a-flash-crowd",), "native_observation", "Observe native per-BSS counts after membership settles; counts do not rank load targets."),
    "beacon_utilization": (("rf-packet-size-counters",), "selected_beacon", "Require a fresh received beacon IE for exact BSSID/receiver/MHz; native AP report is not a substitute."),
    "modeled_busy": (("traffic-low-high-off",), "modeled_survey", "Inspect separate global and radio-local windows; no direct policy input or capacity estimate."),
    "packets_per_second": (("rf-packet-size-counters",), "native_counter", "Fresh owner/epoch-matched delta window gates activity; off phase still has background packets."),
    "bytes_per_second": (("rf-packet-size-counters",), "native_counter", "Observe byte-unit-normalized deltas; do not infer goodput or divide retries by aggregate packets."),
    "retries_per_second": (("rf-asymmetric-ack", "rf-packet-size-counters"), "counter_guard", "Opt-in counter guard vetoes load moves above absolute retry rate; missing counters abstain."),
    "tx_errors_per_second": (("rf-asymmetric-ack",), "counter_guard", "Opt-in AP TX-failure rate guard; zero is valid and not guaranteed to increase."),
    "rx_errors_per_second": (("rf-asymmetric-ack",), "counter_guard", "Opt-in AP RX-drop rate guard; a drop counter is not over-the-air packet loss."),
    "backhaul_hops": (("backhaul-branch-formation", "backhaul-isolation-recovery", "traffic-quieter-ap"), "native_path", "Native valid paths only; unknown/additional hops exclude load targets, geometry never supplies parents."),
    "receive_context": (("received-discovery-recovery",), "received_evidence", "Fresh exact-frequency passive reception qualifies candidates; stale/absent reception cannot be filled from geometry."),
    "room_presence": (("home-a-disappear-reappear", "received-discovery-recovery"), "presence_eligibility", "Absent roles are ineligible; restored presence alone does not establish native ownership or signal."),
    "frequency": (("band-upgrade-24-5", "band-upgrade-5-6", "traffic-quieter-ap"), "context_eligibility", "Check native band/channel, candidate capability and retune floors; same-channel load is shared."),
    "channel_width": (("rf-packet-size-counters",), "context_eligibility", "Inspect configured width; qualified survey requires legacy 20 MHz, unsupported contexts abstain."),
    "packet_types": (("rf-asymmetric-ack", "received-discovery-recovery"), "selected_frames", "Inspect bounded selected management/data/control headers; candidates and injections are different counts."),
    "frame_loss": (("rf-asymmetric-ack",), "selected_frames", "Inspect PER/CCA/off-channel/no-ACK separately; last PER is not measured application loss."),
    "queue_delay": (("rf-packet-size-counters",), "scheduler_observation", "Observe queue/deadline/netlink counters; host delay never becomes a weak-signal decision."),
    "active_rf_modes": (("home-a-stationary", "rf-packet-size-counters"), "model_profile", "Observe actual fading/interference/visibility/priority flags; disabled is not a demonstrated mode."),
    "physical_capacity": (("rf-packet-size-counters",), "unsupported", "Calibrated capacity is unsupported; offered/sender/receiver rates remain separate observations."),
}


def demonstration(property_id):
    rooms, check, expectation = DEMONSTRATIONS[property_id]
    return {"rooms": list(rooms), "check": check, "expectation": expectation,
            "qualification": "contract_defined_live_evidence_required"}
