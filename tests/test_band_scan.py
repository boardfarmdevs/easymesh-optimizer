from datetime import datetime, timedelta, timezone
import threading
from unittest.mock import Mock

import pytest

from optimizer.band_scan import NativeBandScanner, client_radio_lock, frequency_band, parse_capabilities, parse_scan
from optimizer.candidates import CandidateMetricsUnavailable


STATION = "02:00:00:10:01:00"
CURRENT = "02:00:00:00:00:00"
TARGET = "02:00:00:00:01:00"
STAMP = datetime(2026, 9, 13, tzinfo=timezone.utc)


def block(bssid=CURRENT, frequency="2437", signal="-40.00", seen="100.050", ssid="private_ssid"):
    return f"""BSS {bssid}(on wlan0)
\tlast seen: {seen}s [boottime]
\tfreq: {frequency}
\tsignal: {signal} dBm
\tlast seen: 25 ms ago
\tSSID: {ssid}
\tRSN: * Version: 1
\t\t * Authentication suites: PSK SAE
\t\t * Capabilities: MFP-capable MFP-required
"""


def bss_load(stations=0, utilization=0, admission=0):
    return f"""\tBSS Load:
\t\t * station count: {stations}
\t\t * channel utilisation: {utilization}/255
\t\t * available admission capacity: {admission} [*32us]
"""


def parsed(raw):
    return parse_scan(raw, frequencies=[2437, 5180, 6135], started_boottime=100, finished_boottime=100.1)


def test_received_scan_preserves_direction_security_and_integer_or_decimal_frequency():
    samples = parsed(block() + block(TARGET, "5180.0", "-44.00"))
    assert samples[CURRENT]["rcpi"] == 140
    assert samples[TARGET]["rcpi"] == 132
    assert samples[TARGET]["frequency_mhz"] == 5180
    assert samples[TARGET]["key_management"] == ["PSK", "SAE"]
    assert samples[TARGET]["pmf_capable"] and samples[TARGET]["pmf_required"]
    assert samples[TARGET]["advertised_bss_load"] == {"state": "unavailable"}


def test_received_scan_preserves_advertised_bss_load_zero_and_nonzero():
    samples = parsed(block() + bss_load() + block(TARGET) + bss_load(7, 193, 65535))
    assert samples[CURRENT]["advertised_bss_load"] == {
        "state": "available", "station_count": 0, "utilization": 0, "admission_capacity": 0}
    assert samples[TARGET]["advertised_bss_load"] == {
        "state": "available", "station_count": 7, "utilization": 193, "admission_capacity": 65535}


@pytest.mark.parametrize("load", [
    "\tBSS Load:\n\t\t * station count: 1\n",
    bss_load(utilization=256), bss_load(stations=65536), bss_load(admission=65536),
])
def test_incomplete_or_out_of_range_advertised_bss_load_fails_closed(load):
    with pytest.raises(ValueError):
        parsed(block() + load)


def test_scan_ignores_unrequested_frequencies_and_stale_cache():
    assert parsed(block(frequency="5220") + block(TARGET, seen="99.0")) == {}


@pytest.mark.parametrize("raw", [block(seen="101"), block(signal="-111"), block(signal="1"),
                                  block() + block(),
                                  block().replace("[boottime]", "[unknown-clock]"),
                                  block().replace("wlan0", "wlan1")])
def test_invalid_or_ambiguous_scan_does_not_become_a_measurement(raw):
    with pytest.raises(ValueError):
        parsed(raw)


def test_scan_accepts_zero_rcpi_and_rejects_oversized_response():
    assert parsed(block(signal="-110"))[CURRENT]["rcpi"] == 0
    with pytest.raises(ValueError):
        parsed("x" * (512 * 1024 + 1))


def test_hidden_ssid_cannot_poison_scan_or_supply_a_candidate():
    assert parsed(block().replace("SSID: private_ssid", "SSID:")) == {}
    assert parsed(block().replace("SSID: private_ssid", "missing_ssid")) == {}


@pytest.mark.parametrize("frequency,band", [(2412, "2.4"), (5180, "5"), (5975, "6"), (6135, "6")])
def test_frequency_band(frequency, band):
    assert frequency_band(frequency) == band


@pytest.mark.parametrize("frequency", [True, 5180.0, 59000, 0, 5935])
def test_unsupported_frequency_is_explicit(frequency):
    with pytest.raises(ValueError):
        frequency_band(frequency)


@pytest.mark.parametrize("suffix", ["", ".0"])
def test_native_capabilities_exclude_disabled_channels_and_keep_passive_channels(suffix):
    result = parse_capabilities(f"Interface wlan0\n\taddr {STATION}\n\twiphy 9\n",
        f"\t * 2437{suffix} MHz [6] (20.0 dBm)\n\t * 5180{suffix} MHz [36] (20.0 dBm) (no IR)\n"
        f"\t * 5975{suffix} MHz [5] (disabled)\n", "WPA-PSK SAE")
    assert result["frequencies_mhz"] == [2437, 5180]
    assert result["sta_mac"] == STATION and result["phy"] == 9


def scanner(raw=None, after=None, clock_step=0):
    status = f"address={STATION}\nbssid={CURRENT}\nfreq=2437\nssid=private_ssid\nwpa_state=COMPLETED\n"
    commands = []
    status_count = 0

    def command(*arguments):
        nonlocal status_count
        commands.append(arguments)
        if arguments[-1] == "status":
            status_count += 1
            return after if status_count > 1 and after is not None else status
        assert arguments[4:9] == ("iw", "dev", "wlan0", "scan", "freq")
        return raw if raw is not None else block() + block(TARGET, "5180")

    boot = iter([100, 100.1])
    wall = iter([STAMP, STAMP + timedelta(seconds=0.1 + clock_step)])
    native = NativeBandScanner(command, boottime=lambda: next(boot), clock=lambda: next(wall))
    native._scan = Mock(return_value=(raw if raw is not None else block() + block(TARGET, "5180"),
                                     {"started_boottime": 100, "completed_boottime": 100.1, "scan_id": 1,
                                      "before": dict(line.split("=", 1) for line in status.splitlines()),
                                      "after": dict(line.split("=", 1) for line in (after if after is not None else status).splitlines())}))
    return native, commands


def collect(native):
    return native.collect("prpl-client-01", sta_mac=STATION, bssid=CURRENT, ssid="private_ssid",
                          frequencies=[2437, 5180], capabilities={"sta_mac": STATION, "frequencies_mhz": [2437, 5180]})


def test_native_scan_uses_received_timestamps_and_keeps_owner():
    native, commands = scanner()
    result = collect(native)
    assert result["source"] == "client_nl80211_received_scan"
    assert result["samples"][TARGET]["observed_at"] == "2026-09-13T00:00:00.050Z"
    assert result["elapsed_ms"] == 100
    assert not commands
    native._scan.assert_called_once_with("prpl-client-01", STATION, CURRENT, "private_ssid", [2437, 5180])


def test_native_scan_batches_status_and_dump_in_one_namespace_worker():
    import json
    command = Mock(side_effect=[json.dumps({"pid": 1234}),
                                json.dumps({"raw_scan": "BSS dump", "scan_id": 7})])
    native = NativeBandScanner(command)
    raw, metadata = native._scan("prpl-client-01", STATION, CURRENT, "private_ssid", [2437, 5180])
    assert raw == "BSS dump" and metadata == {"scan_id": 7}
    assert command.call_count == 2
    assert command.call_args_list[0].args == ("lxc", "query", "/1.0/instances/prpl-client-01/state")
    assert command.call_args_list[1].args[:5] == ("nsenter", "--target", "1234", "--net", "--")


@pytest.mark.parametrize("options", [{"after": "wpa_state=DISCONNECTED"}, {"raw": block(TARGET, "5180")},
                                      {"raw": block(seen="99")}, {"clock_step": 1}])
def test_native_scan_fails_closed_on_roam_missing_reference_staleness_or_clock_step(options):
    native, _commands = scanner(**options)
    with pytest.raises(CandidateMetricsUnavailable):
        collect(native)


def test_native_scan_does_not_overlap_the_same_station():
    native, commands = scanner()
    native._locks["prpl-client-01"] = threading.Lock()
    native._locks["prpl-client-01"].acquire()
    with pytest.raises(CandidateMetricsUnavailable, match="already in flight"):
        collect(native)
    assert not commands


def test_native_scan_rejects_unbound_container_before_any_command():
    native, commands = scanner()
    with pytest.raises(ValueError, match="bound WLAN"):
        native.capabilities("prpl-controller")
    assert not commands


def test_scans_yield_to_world_band_settings_and_do_not_leave_a_stuck_scan_lock():
    native, commands = scanner()
    with client_radio_lock("prpl-client-01"):
        with pytest.raises(CandidateMetricsUnavailable, match="reserved for band settings"):
            collect(native)
    assert not commands
    assert collect(native)["sta_mac"] == STATION
