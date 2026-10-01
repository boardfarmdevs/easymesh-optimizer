from dataclasses import replace

from optimizer.load_observer import NativeLoadProvider
from .test_load_policy import loaded, SOURCE, STA


def test_late_owner_collection_records_discarded_target_baseline(monkeypatch):
    monkeypatch.setattr("optimizer.load_observer.provenance", lambda *_args: "epoch")
    provider = NativeLoadProvider()
    snapshot = loaded()
    client = snapshot.clients[0]
    source_owner = client.connected_device_id
    target_owner = "02:00:00:00:09:20"
    target = "02:00:00:aa:aa:99"
    raw = {"topology": {"nodes": [{"id": source_owner}, {"id": target_owner}], "edges": []},
           "bsses": {"bsses": [
               {"bssid": bssid, "device_id": owner, "radio_id": bssid,
                "channel": 36, "ssid": "private_ssid"}
               for bssid, owner in ((SOURCE, source_owner), (target, target_owner))]}}
    provider.enrich(snapshot, raw, now_ns=1000000000)
    moved = replace(snapshot, clients=(replace(client, connected_device_id=target_owner,
                                               connected_bssid=target),))

    def ingest(seconds):
        provider.ingest({"source": target_owner, "monotonic_ns": seconds * 1000000000,
                         "received_at": seconds, "loads": [
                             {"bssid": target, "utilization": 10, "station_count": 1}],
                         "traffic": [{"sta_mac": STA, "packets_sent": seconds * 100,
                                      "packets_received": seconds * 100}]})

    ingest(3)
    first = provider.enrich(moved, raw, now_ns=4000000000)
    assert first.client_activity == ()
    reset = raw["load_collection"]["owner_resets"][0]
    assert reset["discarded_report_monotonic_ns"] == 3000000000
    assert reset["floor_monotonic_ns"] == 4000000000
    assert reset["previous_owner"] == (source_owner, SOURCE)
    assert reset["owner"] == (target_owner, target)
    ingest(8)
    assert provider.enrich(moved, raw, now_ns=8000000000).client_activity == ()
    assert raw["load_collection"]["owner_resets"] == []
    ingest(13)
    complete = provider.enrich(moved, raw, now_ns=13000000000)
    assert complete.client_activity[0].interval_seconds == 5
    assert complete.client_activity[0].packets_per_second == 200
