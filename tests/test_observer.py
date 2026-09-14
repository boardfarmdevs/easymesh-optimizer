from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from optimizer.observer import ControllerObserver


def test_topology_projection_does_not_wait_for_any_metric_endpoint_or_probe():
    station = {"staMAC": "02:00:00:00:03:00", "bssid": "02:00:00:00:01:01",
               "band": 1, "ssid": "private_ssid"}
    topology = {"nodes": [{"id": "02:00:00:00:01:20", "name": "Agent-1", "STAList": [station]}]}
    fetcher = Mock(return_value=topology)
    fallback = Mock(side_effect=AssertionError("topology waited for a probe"))
    observer = ControllerObserver("http://controller", fetcher=fetcher, current_link_fallback=fallback)
    result = observer.observe_topology()
    assert result.clients[0].connected_bssid == station["bssid"]
    assert result.clients[0].rcpi is None
    assert result.clients[0].metric_observed_at is None
    assert result.clients[0].cohort == "private"
    fetcher.assert_called_once_with("http://controller/api/v1/topology")
    fallback.assert_not_called()
    topology["nodes"][0]["STAList"].append(station)
    with pytest.raises(ValueError, match="duplicate"):
        observer.observe_topology()


def test_missing_or_wrong_ap_metrics_do_not_remove_clients_or_relabel_measurements():
    station = {"staMAC": "02:00:00:00:03:00", "bssid": "02:00:00:00:01:01", "band": 1}
    fetcher = Mock(return_value={"nodes": [{"id": "02:00:00:00:01:20", "STAList": [station]}]})
    observer = ControllerObserver("http://controller", fetcher=fetcher)
    clients = observer.observe_topology().clients
    for rows in ([], [{"mac": station["staMAC"], "connected_bssid": "02:00:00:00:02:01",
                      "client_metrics": {"rcpi": 120, "last_updated": "2026-09-08T00:00:01Z"}}]):
        fetcher.return_value = {"clients": rows}
        assert observer.metrics_for(clients) == clients


def test_fallback_collection_runs_independent_clients_concurrently():
    stations = [{"staMAC": f"02:00:00:00:{number:02x}:00", "bssid": "02:00:00:00:01:01", "band": 1}
                for number in range(3, 7)]
    fetcher = Mock(return_value={"nodes": [{"id": "02:00:00:00:01:20", "STAList": stations}]})
    observer = ControllerObserver("http://controller", fetcher=fetcher)
    clients = observer.observe_topology().clients
    rendezvous = threading.Barrier(4)
    def fallback(client):
        rendezvous.wait(timeout=2)
        return replace(client, rcpi=120, metric_observed_at="2026-09-08T00:00:01Z")
    observer.current_link_fallback = fallback
    fetcher.return_value = {"clients": []}
    with ThreadPoolExecutor(max_workers=4) as executor:
        observer.fallback_executor = executor
        assert all(client.rcpi == 120 for client in observer.metrics_for(clients))


def test_association_probe_uses_one_native_read_and_rejects_ambiguous_roster():
    station = {'staMAC': '02:00:00:00:03:00', 'bssid': '02:00:00:00:01:01'}
    fetcher = Mock(return_value={'nodes': [{'STAList': [station]}]})
    observer = ControllerObserver('http://controller', fetcher=fetcher)
    assert observer.association(station['staMAC']) == station['bssid']
    fetcher.assert_called_once_with('http://controller/api/v1/topology')
    for stations in ([], [station, station], [{'staMAC': station['staMAC']}]):
        fetcher.return_value = {'nodes': [{'STAList': stations}]}
        assert observer.association(station['staMAC']) is None


def test_ready_client_metric_is_published_before_a_slow_peer_finishes():
    stations = [{"staMAC": f"02:00:00:00:{number:02x}:00", "bssid": "02:00:00:00:01:01", "band": 1}
                for number in (3, 4)]
    fetcher = Mock(return_value={"nodes": [{"id": "02:00:00:00:01:20", "STAList": stations}]})
    observer = ControllerObserver("http://controller", fetcher=fetcher)
    clients = observer.observe_topology().clients
    release = threading.Event()
    published = threading.Event()
    def fallback(client):
        if client == clients[0]:
            assert release.wait(timeout=2)
        return replace(client, rcpi=120, metric_observed_at="2026-09-08T00:00:01Z")
    observer.current_link_fallback = fallback
    observer.current_link_progress = lambda client: published.set() if client.sta_mac == clients[1].sta_mac else None
    fetcher.return_value = {"clients": []}
    with ThreadPoolExecutor(max_workers=2) as probes, ThreadPoolExecutor(max_workers=1) as rounds:
        observer.fallback_executor = probes
        pending = rounds.submit(observer.metrics_for, clients)
        try:
            assert published.wait(timeout=1)
            assert not pending.done()
        finally:
            release.set()
        assert len(pending.result(timeout=1)) == 2


def test_recent_but_pre_movement_serving_metric_is_refreshed_before_candidates():
    now = datetime(2026, 9, 8, 0, 0, 10, tzinfo=timezone.utc)
    payloads = {'topology': {'nodes': []}, 'devices': {'devices': []}, 'bsses': {'bsses': []},
                'clients': {'clients': [{'mac': '02:00:00:00:03:00', 'connected_bssid': '02:00:00:aa:aa:01',
                    'client_metrics': {'rcpi': 100, 'last_updated': '2026-09-08T00:00:08Z'}}]}}
    fallback = Mock(side_effect=lambda client: replace(client, rcpi=80,
        metric_observed_at=now.isoformat(), measurement_source='fresh_kernel_probe'))
    candidates = Mock(return_value=[])
    candidates.last_rejected_candidate_keys = set()
    observer = ControllerObserver('http://controller', fetcher=lambda url: payloads[url.rsplit('/', 1)[-1]],
        clock=lambda: now, max_current_metric_age_seconds=10, current_link_fallback=fallback,
        candidate_provider=candidates, current_metric_floor=lambda client: '2026-09-08T00:00:09Z')
    observer.observe()
    fallback.assert_called_once()
    assert candidates.call_args.args[0][0].rcpi == 80
    assert candidates.call_args.args[0][0].metric_observed_at == now.isoformat()
    fallback.side_effect = lambda client: client
    observer.observe()
    assert candidates.call_args.args[0][0].rcpi is None
    assert candidates.call_args.args[0][0].metric_observed_at is None


def test_opt_in_stale_metric_fallback_keeps_fresh_controller_values_and_raw_evidence():
    observed_at = datetime(2026, 9, 7, 0, 0, 30, tzinfo=timezone.utc)
    clients = [{"mac": f"02:00:00:00:{index:02x}:00", "connected_bssid": "02:00:00:aa:aa:01",
                "client_metrics": {"rcpi": 100, "last_updated": timestamp}}
               for index, timestamp in enumerate(("2026-09-07T00:00:00Z", "2026-09-07T00:00:29Z"), 1)]
    payloads = {"topology": {"nodes": []}, "clients": {"clients": clients},
                "devices": {"devices": []}, "bsses": {"bsses": []}}
    fallback = Mock(side_effect=lambda client: replace(client, rcpi=130,
        metric_observed_at=observed_at.isoformat(), measurement_source="client_kernel_iw_link_after_wlan_traffic_probe"))
    observer = ControllerObserver("http://controller", fetcher=lambda url: payloads[url.rsplit("/", 1)[-1]],
        current_link_fallback=fallback, max_current_metric_age_seconds=15, clock=lambda: observed_at)
    snapshot = observer.observe()
    fallback.assert_called_once()
    assert snapshot.clients[0].rcpi == 130
    assert snapshot.clients[1].rcpi == 100
    assert clients[0]["client_metrics"]["rcpi"] == 100
    assert clients[0]["client_metrics"]["last_updated"] == "2026-09-07T00:00:00Z"


def test_stale_fallback_accounts_for_time_spent_fetching_controller_apis():
    timestamp = "2026-09-07T00:00:00Z"
    payloads = {"topology": {"nodes": []}, "devices": {"devices": []}, "bsses": {"bsses": []},
                "clients": {"clients": [{"mac": "02:00:00:00:03:00",
                    "connected_bssid": "02:00:00:aa:aa:01",
                    "client_metrics": {"rcpi": 100, "last_updated": timestamp}}]}}
    times = iter(datetime(2026, 9, 7, 0, 0, second, tzinfo=timezone.utc)
                 for second in (5, 18, 19))
    fallback = Mock(side_effect=lambda client: replace(client, rcpi=130,
        metric_observed_at="2026-09-07T00:00:18Z",
        measurement_source="client_kernel_iw_link_after_wlan_traffic_probe"))
    observer = ControllerObserver("http://controller",
        fetcher=lambda url: payloads[url.rsplit("/", 1)[-1]],
        current_link_fallback=fallback, max_current_metric_age_seconds=10,
        clock=lambda: next(times))
    snapshot = observer.observe()
    fallback.assert_called_once()
    assert snapshot.clients[0].rcpi == 130
    assert snapshot.clients[0].metric_observed_at == "2026-09-07T00:00:18Z"
    assert payloads["clients"]["clients"][0]["client_metrics"]["last_updated"] == timestamp


def test_optional_kernel_fallback_precedes_candidates_without_rewriting_controller_data():
    timestamp = "2026-08-20T20:00:00Z"
    clients = [{"mac": f"02:00:00:00:0{index}:00", "connected_bssid": "02:00:00:aa:aa:01",
                "client_metrics": {"rcpi": 0 if index == 3 else 100,
                                   "association_uptime_seconds": 90,
                                   "last_updated": timestamp}}
               for index in (3, 4)]
    payloads = {"topology": {"nodes": []}, "clients": {"clients": clients},
                "devices": {"devices": []}, "bsses": {"bsses": []}}
    fallback = Mock(side_effect=lambda client: replace(client, rcpi=110,
                    metric_observed_at="2026-08-20T20:00:01Z", measurement_source="client_kernel_iw_link_after_wlan_traffic_probe"))
    candidates = Mock(return_value=[], last_rejected_candidate_keys=set(), last_raw=[])
    times = iter(datetime(2026, 8, 20, 20, 0, second, tzinfo=timezone.utc) for second in (0, 2, 9))
    observer = ControllerObserver("http://controller",
        fetcher=lambda url: payloads[url.rsplit("/", 1)[-1]],
        current_link_fallback=fallback, candidate_provider=candidates,
        clock=lambda: next(times))
    snapshot = observer.observe()
    fallback.assert_called_once()
    assert snapshot.clients[0].rcpi == 110
    assert snapshot.clients[0].connected_bssid == clients[0]["connected_bssid"]
    assert candidates.call_args.args[0][0].rcpi == 110
    assert candidates.call_args.args[3] == "2026-08-20T20:00:02.000Z"
    assert clients[0]["client_metrics"]["rcpi"] == 0
    assert snapshot.clients[1].measurement_source == "associated_sta_link_metrics"


def test_controller_observer_consumes_controller_report_receipt_time():
    payloads = {
        "/api/v1/topology": {
            "nodes": [
                {
                    "id": "02:00:00:00:09:20",
                    "name": "Extender-1",
                    "STAList": [
                        {
                            "staMAC": "02:00:00:00:03:00",
                            "band": 1,
                            "ssid": "private_ssid",
                        }
                    ],
                    "haulTypes": [],
                }
            ]
        },
        "/api/v1/clients": {
            "clients": [
                {
                    "mac": "02:00:00:00:03:00",
                    "connected_ap_mac": "02:00:00:00:09:20",
                    "connected_bssid": "02:00:00:aa:aa:01",
                    "client_metrics": {
                        "rcpi": 138,
                        "association_uptime_seconds": 42,
                        "last_updated": "2026-08-20T20:00:00Z",
                    },
                }
            ]
        },
        "/api/v1/devices": {
            "devices": [
                {"role": "Controller"},
                {"role": "Agent-1"},
                {"role": "Extender-1"},
                {"role": "Extender-2"},
                {"role": "Extender-3"},
                {"role": "Extender-4"},
            ]
        },
        "/api/v1/bsses": {"bsses": [], "total": 0},
    }

    def fetch(url):
        return payloads[url.removeprefix("http://controller")]

    observer = ControllerObserver(
        "http://controller",
        fetcher=fetch,
        clock=lambda: datetime(2026, 8, 20, 20, 0, tzinfo=timezone.utc),
    )
    result = observer.observe()
    assert result.health.devices == 5
    assert result.health.clients == 1
    assert result.clients[0].rcpi == 138
    assert result.clients[0].band == "5"
    assert result.clients[0].ssid == "private_ssid"
    assert result.clients[0].cohort == "private"
    assert result.clients[0].metric_observed_at == "2026-08-20T20:00:00Z"
    assert result.candidates == ()
    assert observer.last_raw["clients"] is payloads["/api/v1/clients"]


def test_controller_observer_can_reject_timestamp_from_older_image():
    payloads = {
        "/api/v1/topology": {"nodes": []},
        "/api/v1/clients": {"clients": [{
            "mac": "02:00:00:00:03:00",
            "connected_bssid": "02:00:00:aa:aa:01",
            "client_metrics": {
                "rcpi": 138,
                "association_uptime_seconds": 42,
                "last_updated": "2026-08-20T20:00:00Z",
            },
        }]},
        "/api/v1/devices": {"devices": []},
        "/api/v1/bsses": {"bsses": []},
    }
    observer = ControllerObserver(
        "http://controller",
        fetcher=lambda url: payloads[url.removeprefix("http://controller")],
        trust_api_metric_timestamp=False,
        clock=lambda: datetime(2026, 8, 20, 20, 0, tzinfo=timezone.utc),
    )
    assert observer.observe().clients[0].metric_observed_at is None


def test_snapshot_time_is_after_active_candidate_collection():
    payloads = {
        "/api/v1/topology": {"nodes": []},
        "/api/v1/clients": {"clients": []},
        "/api/v1/devices": {"devices": []},
        "/api/v1/bsses": {"bsses": []},
    }
    times = iter([
        datetime(2026, 8, 20, 20, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 20, 20, 0, 9, tzinfo=timezone.utc),
    ])
    provider_calls = []

    def provider(clients, candidates, bsses, sampled_at):
        provider_calls.append(sampled_at)
        return []

    observer = ControllerObserver(
        "http://controller",
        fetcher=lambda url: payloads[url.removeprefix("http://controller")],
        candidate_provider=provider,
        clock=lambda: next(times),
    )
    result = observer.observe()
    assert provider_calls == ["2026-08-20T20:00:00.000Z"]
    assert result.observed_at == "2026-08-20T20:00:09.000Z"
    assert observer.last_raw["sampled_at"] == result.observed_at


def test_controller_inventory_keeps_cross_band_bssid_candidates():
    topology = {
        "nodes": [
            {
                "id": "02:00:00:00:09:20",
                "name": "Extender-1",
                "STAList": [{
                    "staMAC": "02:00:00:00:03:00",
                    "band": 0,
                    "ssid": "private_ssid",
                }],
                "haulTypes": [],
            }
        ]
    }
    payloads = {
        "/api/v1/topology": topology,
        "/api/v1/clients": {"clients": [{
            "mac": "02:00:00:00:03:00",
            "connected_bssid": "02:00:00:aa:aa:01",
            "client_metrics": {"rcpi": 138, "association_uptime_seconds": 42},
        }]},
        "/api/v1/devices": {"devices": [{
            "mac": "02:00:00:00:09:20", "role": "Extender-1",
        }]},
        "/api/v1/bsses": {"bsses": [
            {"bssid": "02:00:00:aa:aa:01", "device_id": "02:00:00:00:09:20",
             "radio_id": "02:00:00:00:09:00", "band": 0, "channel": 6,
             "ssid": "private_ssid", "haul_type": "Fronthaul"},
            {"bssid": "02:00:00:bb:bb:01", "device_id": "02:00:00:00:09:20",
             "radio_id": "02:00:00:00:09:00", "band": 1, "channel": 36,
             "ssid": "private_ssid", "haul_type": "Fronthaul"},
            {"bssid": "02:00:00:cc:cc:01", "device_id": "02:00:00:00:09:20",
             "radio_id": "02:00:00:00:09:00", "band": 3, "channel": 37,
             "ssid": "private_ssid", "haul_type": "Fronthaul"},
        ], "total": 3},
    }
    observer = ControllerObserver(
        "http://controller",
        fetcher=lambda url: payloads[url.removeprefix("http://controller")],
        clock=lambda: datetime(2026, 8, 20, 20, 0, tzinfo=timezone.utc),
    )
    result = observer.observe()
    assert result.clients[0].band == "2.4"
    assert [(item.bssid, item.band) for item in result.candidates] == [
        ("02:00:00:bb:bb:01", "5"),
        ("02:00:00:cc:cc:01", "6"),
    ]
    assert all(item.rcpi is None for item in result.candidates)
    assert all(
        item.measurement_source == "controller_bss_inventory_only"
        for item in result.candidates
    )
    assert all(item.device_name == "Extender-1" for item in result.candidates)


def test_bss_inventory_supplies_client_context_when_topology_lags():
    payloads = {
        "/api/v1/topology": {"nodes": []},
        "/api/v1/clients": {"clients": [{
            "mac": "02:00:00:00:03:00",
            "connected_ap_mac": "02:00:00:00:09:20",
            "connected_bssid": "02:00:00:aa:aa:01",
            "client_metrics": {"rcpi": 138, "association_uptime_seconds": 42},
        }]},
        "/api/v1/devices": {"devices": [{
            "mac": "02:00:00:00:09:20", "role": "Extender-1",
        }]},
        "/api/v1/bsses": {"bsses": [
            {"bssid": "02:00:00:aa:aa:01", "device_id": "02:00:00:00:09:20",
             "radio_id": "02:00:00:00:09:00", "band": 1, "channel": 36,
             "ssid": "private_ssid", "haul_type": "Fronthaul"},
            {"bssid": "02:00:00:bb:bb:01", "device_id": "02:00:00:00:08:20",
             "radio_id": "02:00:00:00:08:00", "band": 1, "channel": 36,
             "ssid": "private_ssid", "haul_type": "Fronthaul"},
        ]},
    }
    observer = ControllerObserver(
        "http://controller",
        fetcher=lambda url: payloads[url.removeprefix("http://controller")],
        clock=lambda: datetime(2026, 8, 20, 20, 0, tzinfo=timezone.utc),
    )

    result = observer.observe()

    assert result.clients[0].band == "5"
    assert result.clients[0].connected_device_name == "Extender-1"
    assert [item.bssid for item in result.candidates] == ["02:00:00:bb:bb:01"]


def test_rejected_candidate_is_excluded_for_one_cycle_then_retried():
    sta = "02:00:00:00:03:00"
    candidate_bssid = "02:00:00:bb:bb:01"
    payloads = {
        "/api/v1/topology": {"nodes": []},
        "/api/v1/clients": {"clients": [{
            "mac": sta,
            "connected_ap_mac": "02:00:00:00:09:20",
            "connected_bssid": "02:00:00:aa:aa:01",
            "client_metrics": {"rcpi": 138, "association_uptime_seconds": 42},
        }]},
        "/api/v1/devices": {"devices": [
            {"mac": "02:00:00:00:09:20", "role": "Extender-1"},
            {"mac": "02:00:00:00:08:20", "role": "Extender-2"},
        ]},
        "/api/v1/bsses": {"bsses": [
            {"bssid": "02:00:00:aa:aa:01", "device_id": "02:00:00:00:09:20",
             "radio_id": "02:00:00:00:09:00", "band": 1, "channel": 36,
             "ssid": "private_ssid", "haul_type": "Fronthaul"},
            {"bssid": candidate_bssid, "device_id": "02:00:00:00:08:20",
             "radio_id": "02:00:00:00:08:00", "band": 1, "channel": 36,
             "ssid": "private_ssid", "haul_type": "Fronthaul"},
        ]},
    }

    class RejectOnce:
        def __init__(self):
            self.calls = 0
            self.last_raw = []
            self.last_rejected_candidate_keys = set()

        def __call__(self, _clients, inventory, _bsses, _sampled_at):
            self.calls += 1
            assert [(item.sta_mac, item.bssid) for item in inventory] == [
                (sta, candidate_bssid)
            ]
            self.last_rejected_candidate_keys = (
                {(sta, candidate_bssid)} if self.calls == 1 else set()
            )
            return []

    provider = RejectOnce()
    observer = ControllerObserver(
        "http://controller",
        fetcher=lambda url: payloads[url.removeprefix("http://controller")],
        candidate_provider=provider,
        clock=lambda: datetime(2026, 8, 20, 20, 0, tzinfo=timezone.utc),
    )

    first = observer.observe()
    assert first.candidates == ()
    assert observer.last_raw["rejected_candidate_keys"] == [
        (sta, candidate_bssid)
    ]

    second = observer.observe()
    assert [(item.sta_mac, item.bssid) for item in second.candidates] == [
        (sta, candidate_bssid)
    ]
    assert provider.calls == 2
