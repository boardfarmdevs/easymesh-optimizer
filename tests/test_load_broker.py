import io
import json
import struct
from types import SimpleNamespace

import pytest

from optimizer.load_capture import PrplBrokerDecoder
from optimizer.load_observer import NativeLoadProvider
from optimizer.model import Snapshot
from .test_load_policy import frame, loaded, tlv, SOURCE, STA


def native_message(*, timestamp=100, interface=2, identifier=1, utilization=0):
    load = tlv(0x94, bytes.fromhex(SOURCE.replace(":", "")) + struct.pack("!BHB", utilization, 3, 0))
    traffic = tlv(0xA2, bytes.fromhex(STA.replace(":", "")) + struct.pack("!IIIIIII", 0, 0, 7, 8, 0, 0, 0))
    ethernet = frame(load + traffic + bytes(3), identifier=identifier)
    metadata = bytearray(40)
    metadata[4:10], metadata[10:16] = ethernet[:6], ethernet[6:12]
    metadata[21] = interface
    struct.pack_into("<HH", metadata, 16, 0x893A, 0x800C)
    struct.pack_into("<H", metadata, 28, len(ethernet) - 14)
    struct.pack_into("<Q", metadata, 32, timestamp)
    body = metadata + ethernet[14:]
    return PrplBrokerDecoder.HEADER.pack(PrplBrokerDecoder.MAGIC, 1, len(body)) + body


def feed(decoder, data, wall=100.75, monotonic_ns=10000000000):
    return decoder.feed(data, received_at=wall, monotonic_ns=monotonic_ns)


def test_subscription_is_only_native_ap_metrics_rx():
    wire = PrplBrokerDecoder.subscription()
    assert struct.unpack_from("<III", wire) == (0xB8C16F47, 3, 260)
    assert wire[12:16] == bytes((0, 1, 1, 0))
    assert struct.unpack_from("<I", wire, 16)[0] == 0x800C0000
    assert wire[20:] == bytes(252)


@pytest.mark.parametrize("split", [1, 11, 12, 25, 51, 64, 100])
def test_stream_boundaries_preserve_native_timestamp_and_true_zero(split):
    decoder = PrplBrokerDecoder()
    wire = native_message()
    assert feed(decoder, wire[:split]) == []
    reports = feed(decoder, wire[split:])
    assert len(reports) == 1
    report = reports[0]
    assert report["transport"] == "prpl-local-broker"
    assert report["received_at"] == 100
    assert report["monotonic_ns"] == 9250000000
    assert report["loads"] == [{"bssid": SOURCE, "utilization": 0, "station_count": 3}]
    assert report["traffic"][0]["packets_sent"] == 7
    assert feed(decoder, wire, wall=101) == []


def test_remote_and_local_reports_share_one_stream_without_ethernet_duplication():
    reports = feed(PrplBrokerDecoder(), native_message() + native_message(
        interface=1, identifier=2, utilization=255))
    assert [report["transport"] for report in reports] == ["prpl-local-broker", "prpl-1905-broker"]
    assert reports[1]["loads"][0]["utilization"] == 255


@pytest.mark.parametrize("wall", [99.99, 105.01, 150])
def test_delayed_or_future_native_reports_cannot_be_retimestamped_as_fresh(wall):
    assert feed(PrplBrokerDecoder(), native_message(), wall=wall) == []


@pytest.mark.parametrize("offset,format,value", [
    (0, "<I", 0), (4, "<I", 2), (8, "<I", 8193), (8, "<I", 47),
    (12, "B", 1), (33, "B", 3), (28, "<H", 0), (30, "<H", 0),
    (40, "<H", 1), (54, ">H", 0x800D),
])
def test_wrong_abi_or_unexpected_message_fails_closed(offset, format, value):
    wire = bytearray(native_message())
    struct.pack_into(format, wire, offset, value)
    with pytest.raises(ValueError, match="unsupported native broker"):
        feed(PrplBrokerDecoder(), wire)


def test_native_stream_memory_is_bounded():
    with pytest.raises(ValueError, match="budget"):
        feed(PrplBrokerDecoder(), bytes(20000))


def test_duplicate_tick_or_older_report_does_not_refresh_load_or_activity():
    provider = NativeLoadProvider()
    report = feed(PrplBrokerDecoder(), native_message())[0]
    provider.ingest(report)
    original_loads, original_traffic = provider.loads.copy(), provider.traffic.copy()
    for timestamp in (99, 100):
        delayed = {**report, "monotonic_ns": report["monotonic_ns"] + 1000000000,
            "received_at": timestamp, "loads": [{**report["loads"][0], "utilization": 255}]}
        provider.ingest(delayed)
        assert provider.loads == original_loads
        assert provider.traffic == original_traffic


def test_cached_local_report_expires_even_while_bridge_and_inventory_remain_fresh(tmp_path):
    status = tmp_path / "bridge.json"
    bridge = {"schema": "easymesh.rf-survey-bridge.v1", "source": "wmediumd-modeled-airtime",
        "instance_id": "one", "recorded_monotonic_ns": 9000000000}
    status.write_text(json.dumps(bridge))
    provider = NativeLoadProvider(provenance_path=status)
    value = loaded()
    owner = value.clients[0].connected_device_id
    raw = {"topology": {"nodes": [{"id": owner}], "edges": []},
        "bsses": {"bsses": [{"bssid": SOURCE, "device_id": owner,
            "radio_id": "02:00:00:01:00:00", "channel": 36, "ssid": "private_ssid"}]}}
    assert provider.enrich(value, raw, now_ns=9000000000).bss_loads == ()
    report = feed(PrplBrokerDecoder(), native_message())[0]
    provider.ingest({**report, "source": owner})
    result = provider.enrich(value, raw, now_ns=10000000000)
    assert result.bss_loads[0].transport == "prpl-local-broker"
    assert result.bss_loads[0].utilization == 0
    status.write_text(json.dumps({**bridge, "recorded_monotonic_ns": 15000000000}))
    assert provider.enrich(value, raw, now_ns=15000000000).bss_loads == ()


@pytest.mark.parametrize("controller,broker", [("prpl-controller", True), ("bpibroadband", False)])
def test_prpl_uses_owned_native_broker_and_rdk_keeps_ethernet(monkeypatch, controller, broker):
    from optimizer import load_observer
    commands = []

    def spawn(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO(),
            stderr=io.StringIO(), wait=lambda **_kwargs: 0)

    monkeypatch.setattr(load_observer.subprocess, "check_output", lambda *_args, **_kwargs: '{"pid":123}')
    monkeypatch.setattr(load_observer.subprocess, "Popen", spawn)
    with pytest.raises(RuntimeError, match="receiver closed"):
        NativeLoadProvider(controller)
    assert ("--broker-socket" in commands[0]) is broker
    if broker:
        assert commands[0][-1] == "/proc/123/root/tmp/beerocks/uds_broker"


def test_transport_provenance_round_trips_and_unknown_transport_is_rejected():
    raw = loaded().to_dict()
    raw["bss_loads"][0]["transport"] = "prpl-local-broker"
    raw["client_activity"][0]["transport"] = "prpl-local-broker"
    assert Snapshot.from_dict(raw).to_dict() == raw
    raw["bss_loads"][0]["transport"] = "cached-nbapi"
    with pytest.raises(ValueError, match="transport"):
        Snapshot.from_dict(raw)
