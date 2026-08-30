from __future__ import annotations

from datetime import datetime, timezone

from optimizer.observer import ControllerObserver


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
