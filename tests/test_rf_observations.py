from dataclasses import replace
from datetime import timedelta

import pytest

from optimizer.load_observer import NativeLoadProvider
from optimizer.model import Snapshot, parse_time
from optimizer.rf_observations import property_catalog, observation_envelope, frequency_for, field_record
from .test_load_policy import loaded, SOURCE


def test_catalog_has_unique_ids_and_separates_diagnostics_from_native_policy():
    catalog = property_catalog()
    definitions = {row["id"]: row for row in catalog["properties"]}
    assert len(definitions) == len(catalog["properties"])
    assert definitions["noise"]["usage"] == "unsupported"
    assert definitions["noise_reference"]["kind"] == "model_parameter"
    assert definitions["configured_snr"]["usage"] == "diagnostic"
    assert definitions["native_utilization"]["usage"] == "load_opt_in"
    catalog["properties"][0]["label"] = "changed"
    assert property_catalog()["properties"][0]["label"] != "changed"


def test_envelope_keeps_valid_zero_units_identity_window_and_schema_compatibility():
    snapshot = loaded()
    snapshot = replace(snapshot, bss_loads=(replace(snapshot.bss_loads[0], utilization=0, station_count=0),))
    original = snapshot.to_dict()
    result = snapshot.rf_observations()
    assert snapshot.to_dict() == original
    assert Snapshot.from_dict(original) == snapshot
    record = result["records"][0]
    assert record["value"] == 0 and record["state"] == "valid"
    assert record["unit"] == "uint8/255"
    assert record["identity"]["bssid"] == SOURCE
    assert record["identity"]["frequency_mhz"] is None
    assert record["window_seconds"] is None
    assert not result["decision_inputs"]
    result["records"][0]["value"] = 255
    assert snapshot.bss_loads[0].utilization == 0


@pytest.mark.parametrize("seconds,state", [(6, "stale"), (-1, "invalid")])
def test_envelope_does_not_publish_stale_or_future_values(seconds, state):
    snapshot = loaded()
    result = observation_envelope(snapshot.bss_loads, now=parse_time(snapshot.observed_at) + timedelta(seconds=seconds))
    assert all(row["value"] is None and row["state"] == state for row in result["records"])


def test_frequency_requires_band_instead_of_guessing_low_channel_numbers():
    assert frequency_for(None, 1) is None
    assert frequency_for("2.4", 1) == 2412
    assert frequency_for("6", 1) == 5955
    assert frequency_for("6", 2) == 5935
    assert frequency_for("5", 36) == 5180
    assert frequency_for("2.4", True) is None


def test_negative_dbm_is_not_confused_with_unsupported_measured_noise():
    now = parse_time(loaded().observed_at)
    arguments = ({}, loaded().observed_at, "model", now)
    assert field_record("noise_reference", -91, *arguments)["value"] == -91
    assert field_record("noise", -91, *arguments)["state"] == "unsupported"
    assert field_record("noise", -91, *arguments)["value"] is None
    assert field_record("native_utilization", 1.5, *arguments)["state"] == "invalid"


def test_cached_inspection_progresses_without_candidate_or_policy_completion(monkeypatch):
    monkeypatch.setattr("optimizer.load_observer.provenance", lambda *_args: "epoch")
    provider = NativeLoadProvider()
    snapshot = loaded()
    owner = snapshot.clients[0].connected_device_id
    now = parse_time(snapshot.observed_at)
    raw = {"topology": {"nodes": [{"id": owner}], "edges": []}, "bsses": {"bsses": [
        {"bssid": SOURCE, "device_id": owner, "radio_id": SOURCE, "channel": 36, "band": 1,
         "ssid": "private_ssid"}]}}
    provider.observe_owners(snapshot.clients, raw, now_ns=1000000000)
    before = dict(provider.client_owners)
    for seconds, utilization in ((2, 0), (3, 255)):
        provider.ingest({"source": owner, "monotonic_ns": seconds * 1000000000,
                         "received_at": (now + timedelta(seconds=seconds)).timestamp(),
                         "loads": [{"bssid": SOURCE, "utilization": utilization, "station_count": 1}], "traffic": []})
        view = provider.inspection(now_ns=seconds * 1000000000, now=now + timedelta(seconds=seconds))
        assert view["error"] is None
        assert view["bss_loads"][0]["utilization"] == utilization
        assert view["bss_loads"][0]["frequency_mhz"] == 5180
        assert view["observations"]["records"][0]["value"] == utilization
    assert provider.client_owners == before
    assert provider.inspection(now_ns=8000000001)["bss_loads"] == []
    monkeypatch.setattr("optimizer.load_observer.provenance", lambda *_args: "replacement")
    assert provider.inspection(now_ns=4000000000)["bss_loads"] == []


def test_passive_reports_continue_with_unknown_context_when_candidates_block(monkeypatch):
    monkeypatch.setattr("optimizer.load_observer.provenance", lambda *_args: "epoch")
    provider = NativeLoadProvider()
    snapshot = loaded()
    now = parse_time(snapshot.observed_at)
    provider.inspection(now_ns=1000000000, now=now)
    provider.ingest({"source": snapshot.clients[0].connected_device_id, "monotonic_ns": 20000000000,
                     "received_at": now.timestamp(), "loads": [
                         {"bssid": SOURCE, "utilization": 99, "station_count": 0}], "traffic": []})
    result = provider.inspection(now_ns=20000000000, now=now)
    assert result["error"] is None
    row = result["bss_loads"][0]
    assert row["utilization"] == 99 and row["context_state"] == "unverified"
    assert row["radio_id"] is None and row["channel"] is None and row["frequency_mhz"] is None
    assert result["observations"]["records"][0]["identity"]["frequency_mhz"] is None
    assert result["client_activity"] == []
    assert provider.epoch is None and not provider.client_owners


def test_live_publication_avoids_expanding_per_property_metadata(monkeypatch):
    monkeypatch.setattr("optimizer.load_observer.provenance", lambda *_args: "epoch")
    def unexpected_expansion(*_args, **_kwargs):
        raise AssertionError("live publication expanded the inspection envelope")
    monkeypatch.setattr("optimizer.load_observer.observation_envelope", unexpected_expansion)
    view = NativeLoadProvider().inspection(now_ns=1000000000, include_envelope=False)
    assert "observations" not in view
    assert view["schema"] == "easymesh.rf-inspection.v2"


def test_snapshot_projection_does_not_change_load_policy_decisions():
    from .test_load_policy import policy
    engine = policy()
    snapshot = loaded()
    before = engine.evaluate(snapshot)
    snapshot.rf_observations()
    assert engine.evaluate(snapshot) == before


def test_backhaul_context_join_is_inspection_only_and_resets_on_retune(monkeypatch):
    monkeypatch.setattr("optimizer.load_observer.provenance", lambda *_args: "epoch")
    provider = NativeLoadProvider()
    snapshot = loaded()
    owner = snapshot.clients[0].connected_device_id
    raw = {"topology": {"nodes": [{"id": owner}], "edges": []}, "bsses": {"bsses": [
        {"bssid": SOURCE, "device_id": owner, "radio_id": SOURCE, "channel": 36, "band": 1,
         "ssid": "mesh_backhaul"}]}}
    provider.observe_owners((), raw, now_ns=1000000000)
    provider.ingest({"source": owner, "monotonic_ns": 2000000000,
                     "received_at": parse_time(snapshot.observed_at).timestamp(),
                     "loads": [{"bssid": SOURCE, "utilization": 0, "station_count": 0}], "traffic": []})
    assert provider.inspection(now_ns=2000000000)["bss_loads"][0]["frequency_mhz"] == 5180
    assert provider.contexts == {}
    raw["bsses"]["bsses"][0]["channel"] = 44
    provider.observe_owners((), raw, now_ns=3000000000)
    load = provider.inspection(now_ns=3000000000)["bss_loads"][0]
    assert load["context_state"] == "unverified" and load["frequency_mhz"] is None
