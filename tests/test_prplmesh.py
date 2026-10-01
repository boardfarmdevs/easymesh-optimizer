import copy
from datetime import datetime, timezone
import threading

import pytest

from optimizer.candidates import CandidateMetricsError, CandidateMetricsUnavailable, CandidateSnapshotSuperseded
from optimizer.model import CandidateObservation, ClientObservation
from optimizer.cli import parser
from optimizer.prplmesh import PrplMeshCandidateProvider, PrplMeshObserver, _first_json


NOW = datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc)
STAMP = "2026-08-28T18:00:00.000Z"
METRIC_STAMP = "2026-08-28T17:59:57.125Z"


def test_candidate_registration_is_bounded_parallel_and_cached(monkeypatch):
    provider = PrplMeshCandidateProvider(allow_simulated=True)
    targets = {(f"radio-{index // 4}", f"station-{index}"): [] for index in range(8)}
    metadata = {radio: {"channel": 36, "opclass": 115, "device_id": radio} for radio, _ in targets}
    lock = threading.Lock()
    release = threading.Event()
    active = peak = 0
    calls = []

    def call(radio, method, request):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            calls.append((radio, request["un_station_mac"]))
            if active == 4:
                release.set()
        assert release.wait(2), "registration was serialized"
        assert method == "AddUnassociatedStation"
        with lock:
            active -= 1
        return {"retval": ""}

    monkeypatch.setattr(provider, "_call", call)
    provider._register_targets(targets, metadata)
    assert peak == 4 and active == 0
    assert set(calls) == provider.registered == set(targets)
    assert len(calls) == 8
    provider._register_targets(targets, metadata)
    assert len(calls) == 8


def test_registration_failure_keeps_only_successes_and_retries_missing(monkeypatch):
    provider = PrplMeshCandidateProvider(allow_simulated=True)
    targets = {("radio", f"station-{index}"): [] for index in range(8)}
    metadata = {"radio": {"channel": 36, "opclass": 115, "device_id": "agent"}}
    successes = set()
    failed = True

    def call(radio, method, request):
        if failed and request["un_station_mac"] == "station-0":
            raise CandidateMetricsUnavailable("injected registration failure")
        key = (radio, request["un_station_mac"])
        assert key not in successes
        successes.add(key)
        return {"retval": ""}

    monkeypatch.setattr(provider, "_call", call)
    with pytest.raises(CandidateMetricsUnavailable, match="injected registration failure"):
        provider._register_targets(targets, metadata)
    assert provider.registered == successes
    assert ("radio", "station-0") not in provider.registered
    failed = False
    provider._register_targets(targets, metadata)
    assert provider.registered == successes == set(targets)


def test_registration_rebinds_station_when_returning_to_previous_band(monkeypatch):
    provider = PrplMeshCandidateProvider(allow_simulated=True)
    native = {}
    calls = []

    def call(radio, method, request):
        native[request["un_station_mac"]] = (request["channel"], request["operating_class"])
        calls.append((radio, request["un_station_mac"]))
        return {"retval": ""}

    monkeypatch.setattr(provider, "_call", call)
    metadata = {"radio-24": {"channel": 6, "opclass": 81, "device_id": "agent"},
                "radio-5": {"channel": 36, "opclass": 115, "device_id": "agent"}}
    provider._register_targets({("radio-24", "unchanged"): []}, metadata)
    for radio in ("radio-24", "radio-5", "radio-24"):
        provider._register_targets({(radio, "moving"): []}, metadata)
        assert native["moving"] == (metadata[radio]["channel"], metadata[radio]["opclass"])
    provider._register_targets({("radio-24", "moving"): [], ("radio-24", "unchanged"): []}, metadata)
    assert calls == [("radio-24", "unchanged"), ("radio-24", "moving"),
                     ("radio-5", "moving"), ("radio-24", "moving")]


@pytest.mark.parametrize("changed", [{"channel": 44}, {"opclass": 128}])
def test_registration_tracks_channel_and_operating_class(monkeypatch, changed):
    provider = PrplMeshCandidateProvider(allow_simulated=True)
    calls = []
    monkeypatch.setattr(provider, "_call", lambda *args: calls.append(args) or {"retval": ""})
    targets = {("radio", "station"): []}
    metadata = {"radio": {"channel": 36, "opclass": 115, "device_id": "agent"}}
    provider._register_targets(targets, metadata)
    metadata["radio"].update(changed)
    provider._register_targets(targets, metadata)
    provider._register_targets(targets, metadata)
    assert len(calls) == 2
    assert calls[-1][2]["channel"] == metadata["radio"]["channel"]
    assert calls[-1][2]["operating_class"] == metadata["radio"]["opclass"]


def test_registration_supersession_does_not_call_native_api(monkeypatch):
    provider = PrplMeshCandidateProvider(allow_simulated=True, generation_guard=lambda: False)
    monkeypatch.setattr("optimizer.prplmesh.subprocess.run", lambda *args, **kwargs: pytest.fail("native call after supersession"))
    with pytest.raises(CandidateSnapshotSuperseded):
        provider._register_targets({("radio", "station"): []},
                                   {"radio": {"channel": 36, "opclass": 115, "device_id": "agent"}})
    assert not provider.registered and not provider.last_raw


def _topology():
    return {
        "generated_at": STAMP,
        "devices": [
            {
                "id": "02:00:00:27:01:01",
                "name": "controller",
                "radios": [
                    {
                        "id": "02:00:00:00:01:00",
                        "band": "5 GHz",
                        "channel": 36,
                        "opclass": 115,
                        "bsses": [
                            {
                                "bssid": "02:00:00:00:01:00",
                                "ssid": "private_ssid",
                                "clients": [
                                    {
                                        "id": "02:00:00:10:01:00",
                                        "signal_raw": 88,
                                        "signal_updated_at": METRIC_STAMP,
                                        "last_connect_seconds": 42,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
            {
                "id": "02:00:00:27:02:01",
                "name": "agent-1",
                "radios": [
                    {
                        "id": "02:00:00:00:04:00",
                        "band": "5 GHz",
                        "channel": 36,
                        "opclass": 115,
                        "bsses": [
                            {
                                "bssid": "02:00:00:00:04:00",
                                "ssid": "private_ssid",
                                "clients": [],
                            }
                        ],
                    }
                ],
            },
        ],
    }


def test_observer_normalizes_prplmesh_topology_without_inventing_candidate_metrics():
    observer = PrplMeshObserver(fetcher=lambda _url: _topology(), clock=lambda: NOW)
    snapshot = observer.observe()
    assert snapshot.health.devices == 2
    assert snapshot.health.radios == 2
    assert snapshot.health.bsses == 2
    assert snapshot.health.clients == 1
    assert snapshot.clients[0].rcpi == 88
    assert snapshot.clients[0].metric_observed_at == METRIC_STAMP
    assert snapshot.clients[0].band == "5"
    assert snapshot.clients[0].cohort == "private"
    assert len(snapshot.candidates) == 1
    assert snapshot.candidates[0].bssid == "02:00:00:00:04:00"
    assert snapshot.candidates[0].rcpi is None
    assert snapshot.candidates[0].measurement_source == "prplmesh_bss_inventory_only"


def test_observer_does_not_replace_missing_metric_time_with_http_sample_time():
    topology = copy.deepcopy(_topology())
    del topology["devices"][0]["radios"][0]["bsses"][0]["clients"][0][
        "signal_updated_at"
    ]
    observer = PrplMeshObserver(fetcher=lambda _url: topology, clock=lambda: NOW)
    assert observer.observe().clients[0].metric_observed_at is None


def test_observer_retries_non_atomic_roaming_snapshot(monkeypatch):
    duplicate = _topology()
    duplicate["devices"][1]["radios"][0]["bsses"][0]["clients"] = copy.deepcopy(
        duplicate["devices"][0]["radios"][0]["bsses"][0]["clients"])
    snapshots = iter([duplicate, _topology()])
    monkeypatch.setattr("optimizer.prplmesh.time.sleep", lambda seconds: None)
    observer = PrplMeshObserver(fetcher=lambda url: next(snapshots), clock=lambda: NOW)
    assert observer.observe().health.clients == 1


def test_ambiguous_ownership_and_transport_failure_are_unavailable(monkeypatch):
    duplicate = _topology()
    duplicate["devices"][1]["radios"][0]["bsses"][0]["clients"] = copy.deepcopy(
        duplicate["devices"][0]["radios"][0]["bsses"][0]["clients"])
    monkeypatch.setattr("optimizer.prplmesh.time.sleep", lambda seconds: None)
    observer = PrplMeshObserver(fetcher=lambda url: duplicate, clock=lambda: NOW)
    with pytest.raises(CandidateMetricsUnavailable, match="duplicate client ownership"):
        observer.observe()
    assert observer.last_raw is None

    def unavailable(url):
        raise OSError("temporary adapter interruption")

    observer = PrplMeshObserver(fetcher=unavailable, clock=lambda: NOW)
    with pytest.raises(CandidateMetricsUnavailable, match="collection failed: OSError: temporary adapter interruption"):
        observer.observe()


def test_candidate_provider_requires_explicit_simulated_metric_authority():
    provider = PrplMeshCandidateProvider()
    with pytest.raises(CandidateMetricsError, match="allow-simulated-candidates"):
        list(provider((), (), [], STAMP))

def test_same_second_publication_is_not_mistaken_for_stale_native_metrics(monkeypatch):
    provider = PrplMeshCandidateProvider(allow_simulated=True)
    elapsed = [0.25]
    epoch = NOW.timestamp()
    monkeypatch.setattr("optimizer.prplmesh.time.time", lambda: epoch + elapsed[0])
    monkeypatch.setattr("optimizer.prplmesh.time.monotonic", lambda: elapsed[0])
    monkeypatch.setattr("optimizer.prplmesh.time.sleep", lambda delay: elapsed.__setitem__(0, elapsed[0] + delay))
    provider._await_timestamp_resolution({("radio", "sta"): (100, STAMP)})
    assert elapsed[0] == pytest.approx(1)
    assert provider.last_raw[-1]["elapsed_ms"] == pytest.approx(750)
    provider.last_raw.clear()
    provider._await_timestamp_resolution({("radio", "sta"): (100, STAMP)})
    assert provider.last_raw == []

def test_bulk_radio_read_uses_one_native_query_without_cross_radio_leakage(monkeypatch):
    provider = PrplMeshCandidateProvider(allow_simulated=True)
    radios = [provider.NETWORK + ".Device.1.Radio.1", provider.NETWORK + ".Device.2.Radio.1"]
    calls = []
    def call(obj, method, payload):
        calls.append((obj, method, payload))
        return {
            radio + ".UnassociatedSTA.1.": {"MACAddress": "02:00:00:10:01:00",
                "SignalStrength": 100 + index, "X_PRPLWARE-COM_TimeStamp": STAMP}
            for index, radio in enumerate(radios)
        }
    monkeypatch.setattr(provider, "_call", call)
    result = provider._read_radios(radios)
    assert len(calls) == 1
    assert calls[0][2]["rel_path"] == "Device.*.Radio.*.UnassociatedSTA."
    assert result[radios[0]]["02:00:00:10:01:00"][0] == 100
    assert result[radios[1]]["02:00:00:10:01:00"][0] == 101


@pytest.mark.parametrize("signal", [0, 1, 220, "0"])
def test_fresh_candidate_includes_valid_rcpi_endpoints(signal):
    values = {"Radio.1.UnassociatedSTA.1.": {
        "MACAddress": "02:00:00:10:01:00", "SignalStrength": signal,
        "X_PRPLWARE-COM_TimeStamp": STAMP,
    }}
    assert PrplMeshCandidateProvider._parse_radio_metrics(values) == {
        "02:00:00:10:01:00": (int(signal), STAMP),
    }


@pytest.mark.parametrize("signal", [None, "", "invalid", -1, 221, 255, True, 0.5])
def test_candidate_rejects_missing_reserved_or_malformed_rcpi(signal):
    values = {"Radio.1.UnassociatedSTA.1.": {
        "MACAddress": "02:00:00:10:01:00", "SignalStrength": signal,
        "X_PRPLWARE-COM_TimeStamp": STAMP,
    }}
    assert PrplMeshCandidateProvider._parse_radio_metrics(values) == {}


@pytest.mark.parametrize("timestamp", [None, "0001-01-01T00:00:00Z"])
def test_zero_candidate_without_native_timestamp_is_not_a_measurement(timestamp):
    values = {"Radio.1.UnassociatedSTA.1.": {
        "MACAddress": "02:00:00:10:01:00", "SignalStrength": 0,
        "X_PRPLWARE-COM_TimeStamp": timestamp,
    }}
    assert PrplMeshCandidateProvider._parse_radio_metrics(values) == {}


def test_serving_zero_rcpi_is_weak_not_missing():
    topology = _topology()
    topology["devices"][0]["radios"][0]["bsses"][0]["clients"][0]["signal_raw"] = 0
    observer = PrplMeshObserver(fetcher=lambda url: topology, clock=lambda: NOW)
    assert observer.observe().clients[0].rcpi == 0


class _Provider(PrplMeshCandidateProvider):
    def __init__(self):
        super().__init__(allow_simulated=True, timeout_seconds=0.01, batch_radio_reads=False)
        self.calls = []

    def _radio_objects(self):
        return {
            ("02:00:00:27:02:01", "02:00:00:00:04:00"):
                "Device.WiFi.DataElements.Network.Device.2.Radio.2"
        }

    def _call(self, obj, method, payload):
        self.calls.append((obj, method, payload))
        return {"status": "ok"}

    def _radio_metrics(self, radio):
        assert radio.endswith("Radio.2")
        updates = sum(method == "UpdateUnassociatedStationsStats" for _, method, _ in self.calls)
        timestamp = STAMP if updates else METRIC_STAMP
        return {"02:00:00:10:01:00": (122, timestamp)}


def test_radio_discovery_caches_inventory_and_capability_without_publishing_failed_cache(monkeypatch):
    provider = PrplMeshCandidateProvider()
    device = provider.NETWORK + ".Device.1"
    radio_one, radio_two = device + ".Radio.1", device + ".Radio.2"
    values = {device: {"ID": "02:00:00:27:01:01"}, radio_one: {"ID": "02:00:00:00:01:00"},
              radio_two: {"ID": "02:00:00:00:02:00"}}
    calls = []
    def interrupted_read(obj, method, payload):
        calls.append((obj, method, payload))
        raise CandidateSnapshotSuperseded("world changed during discovery")
    monkeypatch.setattr(provider, "_call", interrupted_read)
    with pytest.raises(CandidateSnapshotSuperseded):
        provider._radio_objects()
    assert provider.object_cache == {}
    def completed_read(*args):
        calls.append(args)
        return values
    monkeypatch.setattr(provider, "_call", completed_read)
    assert len(provider._radio_objects()) == 2
    assert len(provider._radio_objects()) == 2
    inventory_call = (provider.NETWORK, "_get", {"rel_path": "Device.*.", "depth": 2})
    assert calls == [inventory_call, inventory_call,
                     (radio_one, "_describe", {"functions": True, "parameters": False, "objects": False}),
                     inventory_call]
    assert not provider.defer_registration_query


@pytest.mark.parametrize("new_device,new_radio", [(7, 1), (2, 1)])
def test_candidate_registration_follows_recreated_native_radio_paths(monkeypatch, new_device, new_radio):
    provider = PrplMeshCandidateProvider(allow_simulated=True)
    device_id = "02:00:00:27:02:01"
    radio_id = "02:00:00:00:04:00"
    old_device = provider.NETWORK + ".Device.2"
    old_radio = old_device + ".Radio.2"
    current_device = old_device
    current_radio = old_radio
    calls = []

    def call(obj, method, payload):
        calls.append((obj, method, payload))
        if method == "_get":
            return {current_device: {"ID": device_id}, current_radio: {"ID": radio_id}}
        if method == "_describe":
            return {"functions": {"AddUnassociatedStation": {"arguments": [
                {"name": "defer_query", "type_name": "bool"}]}}}
        assert obj == current_radio
        return {"retval": ""}

    monkeypatch.setattr(provider, "_call", call)
    metadata = {"channel": 36, "opclass": 115, "device_id": device_id}
    assert provider._radio_objects()[(device_id, radio_id)] == old_radio
    provider._register_targets({(old_radio, "station"): []}, {old_radio: metadata})
    provider._radio_objects()
    assert provider.registered == {(old_radio, "station")}
    assert sum(method == "_describe" for _, method, _ in calls) == 1
    current_device = provider.NETWORK + f".Device.{new_device}"
    current_radio = current_device + f".Radio.{new_radio}"
    assert provider._radio_objects()[(device_id, radio_id)] == current_radio
    assert not provider.registered and not provider.registration_channels
    provider._register_targets({(current_radio, "station"): []}, {current_radio: metadata})
    assert provider.registered == {(current_radio, "station")}
    assert [obj for obj, method, _ in calls if method == "AddUnassociatedStation"] == [
        old_radio, current_radio]


def test_failed_rediscovery_cannot_reuse_old_registrations(monkeypatch):
    provider = PrplMeshCandidateProvider()
    provider.object_cache = {("agent", "radio"): "old-path"}
    provider.registered = {("old-path", "station")}
    provider.registration_channels = {"station": frozenset({(36, 115)})}
    device = provider.NETWORK + ".Device.7"

    def call(obj, method, payload):
        if method == "_describe":
            raise CandidateMetricsUnavailable("radio disappeared during rediscovery")
        return {device: {"ID": "02:00:00:27:02:01"},
                device + ".Radio.1": {"ID": "02:00:00:00:04:00"}}

    monkeypatch.setattr(provider, "_call", call)
    with pytest.raises(CandidateMetricsUnavailable, match="disappeared"):
        provider._radio_objects()
    assert not provider.object_cache and not provider.registered and not provider.registration_channels


def test_orphaned_radio_does_not_leave_a_partial_inventory(monkeypatch):
    provider = PrplMeshCandidateProvider()
    device = provider.NETWORK + ".Device.1"
    values = {device: {"ID": "02:00:00:27:01:01"},
              device + ".Radio.1": {"ID": "02:00:00:00:01:00"},
              provider.NETWORK + ".Device.2.Radio.1": {"ID": "02:00:00:00:02:00"}}
    monkeypatch.setattr(provider, "_call", lambda *args: values)
    with pytest.raises(CandidateMetricsUnavailable, match="no discovered parent"):
        provider._radio_objects()
    assert provider.object_cache == {}


@pytest.mark.parametrize("type_name,supported", [("bool", True), ("cstring_t", False), (None, False)])
def test_registration_deferral_requires_native_boolean_capability(monkeypatch, type_name, supported):
    provider = PrplMeshCandidateProvider()
    device = provider.NETWORK + ".Device.1"
    radio = device + ".Radio.1"
    requests = []

    def call(obj, method, payload):
        requests.append((obj, method, payload))
        if method == "_get":
            return {device: {"ID": "02:00:00:27:01:01"}, radio: {"ID": "02:00:00:00:01:00"}}
        if method == "_describe":
            return {"functions": {"AddUnassociatedStation": {"arguments": [
                {"name": "defer_query", "type_name": type_name}]}}}
        return {"retval": ""}

    monkeypatch.setattr(provider, "_call", call)
    provider._radio_objects()
    assert provider.defer_registration_query is supported
    provider._register_targets({(radio, "station"): []},
                               {radio: {"channel": 36, "opclass": 115, "device_id": "agent"}})
    assert requests[-1][1] == "AddUnassociatedStation"
    assert requests[-1][2].get("defer_query", False) is supported
    assert all(method != "UpdateUnassociatedStationsStats" for _object, method, _payload in requests)


def test_capability_discovery_failure_does_not_cache_inventory(monkeypatch):
    provider = PrplMeshCandidateProvider()
    device = provider.NETWORK + ".Device.1"

    def call(obj, method, payload):
        if method == "_describe":
            raise CandidateMetricsUnavailable("introspection failed")
        return {device: {"ID": "02:00:00:27:01:01"},
                device + ".Radio.1": {"ID": "02:00:00:00:01:00"}}

    monkeypatch.setattr(provider, "_call", call)
    with pytest.raises(CandidateMetricsUnavailable, match="introspection failed"):
        provider._radio_objects()
    assert not provider.object_cache and not provider.registered


def test_candidate_provider_registers_updates_and_returns_standard_metric():
    provider = _Provider()
    client = ClientObservation(
        sta_mac="02:00:00:10:01:00",
        connected_device_id="02:00:00:27:01:01",
        connected_device_name="controller",
        connected_bssid="02:00:00:00:01:00",
        rcpi=88,
        association_uptime_seconds=42,
        metric_observed_at=STAMP,
        measurement_source="prplmesh_associated_sta_link_metrics",
        band="5",
        ssid="private_ssid",
        cohort="private",
    )
    candidate = CandidateObservation(
        sta_mac=client.sta_mac,
        bssid="02:00:00:00:04:00",
        device_id="02:00:00:27:02:01",
        device_name="agent-1",
        rcpi=None,
        metric_observed_at=None,
        measurement_source="prplmesh_bss_inventory_only",
        band="5",
    )
    bss = {
        "bssid": candidate.bssid,
        "device_id": candidate.device_id,
        "radio_id": "02:00:00:00:04:00",
        "channel": 36,
        "opclass": 115,
    }
    published = []
    provider.result_ready = lambda measurements, rejected, transaction: published.append(
        (measurements, rejected, transaction))
    measured = list(provider((client,), (candidate,), [bss], STAMP))
    assert len(measured) == 1
    assert measured[0].rcpi == 122
    assert measured[0].metric_observed_at == STAMP
    assert measured[0].measurement_source.endswith(":simulated")
    assert published[0][0] == measured
    assert published[0][1] == set()
    assert published[0][2]["operation"] == "published"
    assert any(method == "AddUnassociatedStation" for _, method, _ in provider.calls)
    assert any(method == "UpdateUnassociatedStationsStats" for _, method, _ in provider.calls)


def test_candidate_provider_can_limit_measurement_to_selected_client():
    provider = _Provider()
    sta_mac = "02:00:00:10:01:00"
    provider.client_selector = lambda client, _observed_at: client.sta_mac != sta_mac
    client = ClientObservation(
        sta_mac=sta_mac,
        connected_device_id="02:00:00:27:01:01",
        connected_device_name="controller",
        connected_bssid="02:00:00:00:01:00",
        rcpi=88,
        association_uptime_seconds=42,
        metric_observed_at=STAMP,
        measurement_source="prplmesh_associated_sta_link_metrics",
        band="5",
        ssid="private_ssid",
        cohort="private",
    )
    candidate = CandidateObservation(
        sta_mac=sta_mac,
        bssid="02:00:00:00:04:00",
        device_id="02:00:00:27:02:01",
        device_name="agent-1",
        rcpi=None,
        metric_observed_at=None,
        measurement_source="prplmesh_bss_inventory_only",
        band="5",
    )
    bss = {
        "bssid": candidate.bssid,
        "device_id": candidate.device_id,
        "radio_id": "02:00:00:00:04:00",
        "channel": 36,
        "opclass": 115,
    }

    assert list(provider((client,), (candidate,), [bss], STAMP)) == []
    assert not any(method == "AddUnassociatedStation" for _, method, _ in provider.calls)


def test_candidate_provider_rejects_cached_metrics_after_update():
    provider = _Provider()
    provider._radio_metrics = lambda radio: {"02:00:00:10:01:00": (122, METRIC_STAMP)}
    observer = PrplMeshObserver(fetcher=lambda url: _topology(),
                                candidate_provider=provider, clock=lambda: NOW)
    with pytest.raises(CandidateMetricsError, match="incomplete"):
        observer.observe()
    incomplete = provider.last_raw[-1]
    assert incomplete["operation"] == "incomplete"
    assert incomplete["last_read_at"]
    assert incomplete["freshness"] == [{
        "radio": "Device.WiFi.DataElements.Network.Device.2.Radio.2",
        "sta_mac": "02:00:00:10:01:00",
        "baseline": (122, METRIC_STAMP), "last_read": (122, METRIC_STAMP),
    }]


def test_candidate_timeout_distinguishes_missing_from_unchanged_publication():
    provider = _Provider()
    provider._radio_metrics = lambda radio: {}
    observer = PrplMeshObserver(fetcher=lambda url: _topology(),
                                candidate_provider=provider, clock=lambda: NOW)
    with pytest.raises(CandidateMetricsUnavailable, match="incomplete"):
        observer.observe()
    assert provider.last_raw[-1]["freshness"][0]["last_read"] is None
    assert provider.last_raw[-1]["freshness"][0]["baseline"] is None


@pytest.fixture
def retry_provider(monkeypatch):
    elapsed = [0.0]
    monkeypatch.setattr("optimizer.prplmesh.time.monotonic", lambda: elapsed[0])
    monkeypatch.setattr("optimizer.prplmesh.time.sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))
    provider = _Provider()
    provider.timeout_seconds = 30
    observer = PrplMeshObserver(fetcher=lambda url: _topology(), candidate_provider=provider, clock=lambda: NOW)
    return provider, observer, elapsed


def test_lost_candidate_query_retries_without_reusing_stale_values(retry_provider):
    provider, observer, elapsed = retry_provider
    published = []
    provider.result_ready = lambda measurements, rejected, transaction: published.extend(measurements)

    def read(radio):
        updates = sum(method == "UpdateUnassociatedStationsStats" for _, method, _ in provider.calls)
        return {"02:00:00:10:01:00": (122, STAMP if updates >= 2 else METRIC_STAMP)}

    provider._radio_metrics = read
    snapshot = observer.observe()
    assert 2 <= elapsed[0] < 2.3
    assert len(snapshot.candidates) == len(published) == 1
    assert snapshot.candidates[0].metric_observed_at == STAMP
    retries = [entry for entry in provider.last_raw if entry["operation"] == "update_retry"]
    assert len(retries) == 1
    assert retries[0]["attempt"] == 1
    assert retries[0]["missing"] == [("Device.WiFi.DataElements.Network.Device.2.Radio.2", "02:00:00:10:01:00")]
    assert sum(method == "AddUnassociatedStation" for _, method, _ in provider.calls) == 1


@pytest.mark.parametrize("timeout,expected_retries", [(1, 0), (5, 1), (30, 3), (120, 3)])
def test_candidate_retry_budget_never_extends_deadline(retry_provider, timeout, expected_retries):
    provider, observer, elapsed = retry_provider
    provider.timeout_seconds = timeout
    provider._radio_metrics = lambda radio: {"02:00:00:10:01:00": (122, METRIC_STAMP)}
    with pytest.raises(CandidateMetricsUnavailable, match="incomplete"):
        observer.observe()
    assert elapsed[0] == pytest.approx(timeout)
    assert sum(method == "UpdateUnassociatedStationsStats" for _, method, _ in provider.calls) == expected_retries + 1
    assert provider.last_raw[-1]["freshness"][0]["last_read"] == (122, METRIC_STAMP)
    assert not any(entry["operation"] == "published" for entry in provider.last_raw)


def test_candidate_retry_stops_when_world_is_superseded(retry_provider):
    provider, observer, elapsed = retry_provider
    provider.generation_guard = lambda: elapsed[0] < 1.5
    provider._radio_metrics = lambda radio: {}
    with pytest.raises(CandidateSnapshotSuperseded):
        observer.observe()
    assert elapsed[0] < 2
    assert sum(method == "UpdateUnassociatedStationsStats" for _, method, _ in provider.calls) == 1


def test_candidate_retry_does_not_republish_partial_results(retry_provider):
    provider, observer, elapsed = retry_provider
    topology = _topology()
    second_client = copy.deepcopy(topology["devices"][0]["radios"][0]["bsses"][0]["clients"][0])
    second_client["id"] = "02:00:00:10:02:00"
    topology["devices"][0]["radios"][0]["bsses"][0]["clients"].append(second_client)
    observer = PrplMeshObserver(fetcher=lambda url: topology, candidate_provider=provider, clock=lambda: NOW)
    published = []
    provider.result_ready = lambda measurements, rejected, transaction: published.extend(measurements)

    def read(radio):
        updates = sum(method == "UpdateUnassociatedStationsStats" for _, method, _ in provider.calls)
        return {"02:00:00:10:01:00": (122, STAMP if updates else METRIC_STAMP),
                second_client["id"]: (118, STAMP if updates >= 2 else METRIC_STAMP)}

    provider._radio_metrics = read
    snapshot = observer.observe()
    assert 2 <= elapsed[0] < 2.3
    assert len(snapshot.candidates) == len(published) == 2
    assert len({entry.sta_mac for entry in published}) == 2


def test_zero_candidate_completes_only_after_native_timestamp_advances():
    provider = _Provider()
    def read(radio):
        updated = any(method == "UpdateUnassociatedStationsStats" for _, method, _ in provider.calls)
        return provider._parse_radio_metrics({radio + ".UnassociatedSTA.1.": {
            "MACAddress": "02:00:00:10:01:00", "SignalStrength": 0,
            "X_PRPLWARE-COM_TimeStamp": STAMP if updated else METRIC_STAMP,
        }})
    provider._radio_metrics = read
    observer = PrplMeshObserver(fetcher=lambda url: _topology(), candidate_provider=provider, clock=lambda: NOW)
    assert observer.observe().candidates[0].rcpi == 0
    assert provider.last_raw[-1]["operation"] == "complete"
    provider.calls.clear()
    provider._radio_metrics = lambda radio: {"02:00:00:10:01:00": (0, METRIC_STAMP)}
    with pytest.raises(CandidateMetricsUnavailable, match="incomplete"):
        observer.observe()


def test_missing_serving_metric_uses_only_explicit_fallback():
    from dataclasses import replace

    topology = _topology()
    topology["devices"][0]["radios"][0]["bsses"][0]["clients"][0]["signal_raw"] = None
    observer = PrplMeshObserver(fetcher=lambda url: topology, clock=lambda: NOW,
        current_link_fallback=lambda client: replace(client, rcpi=90,
            metric_observed_at=STAMP, measurement_source="test_kernel_sample"))
    sample = observer.observe().clients[0]
    assert sample.rcpi == 90
    assert sample.measurement_source == "test_kernel_sample"


def test_first_json_accepts_ubus_prefix_noise_and_rejects_non_objects():
    assert _first_json('  {"answer": 42}\nwarning') == {"answer": 42}
    with pytest.raises(CandidateMetricsError):
        _first_json("[]")


def test_live_candidate_timeout_is_operator_configurable():
    args = parser().parse_args([
        "recommend", "--journal", "/tmp/journal.jsonl",
        "--policy", "configs/threshold-policy.yaml",
        "--candidate-timeout", "300",
    ])
    assert args.candidate_timeout == 300


def test_live_candidate_timeout_must_be_positive():
    with pytest.raises(SystemExit):
        parser().parse_args([
            "recommend", "--journal", "/tmp/journal.jsonl",
            "--policy", "configs/threshold-policy.yaml",
            "--candidate-timeout", "0",
        ])
