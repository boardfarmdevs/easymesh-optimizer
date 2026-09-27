from unittest.mock import Mock, patch

import pytest

from room_demo.conductor import LiveConductor
from room_demo.pool import pool_manifest


def preflight(band, interactive, *, traffic=True, clients=20, ssid="private_ssid", rcpi=120,
              capacity=20, private=10, iot=10):
    conductor = Mock(interactive=interactive)
    conductor.manifest = {"hero": {"expected_band": "5", "expected_ssid": "private_ssid"},
                          "health": {"expected_mesh_devices": 5, "expected_clients": 20,
                                     "expected_private_clients": 10, "expected_iot_clients": 10}}
    if interactive:
        conductor.manifest = pool_manifest(conductor.manifest, {"bindings": {
            f"station_{ordinal}": {"role_type": "station"} for ordinal in range(capacity)}})
    conductor._network_payload.return_value = {"hero": {"band": band, "ssid": ssid, "rcpi": rcpi},
                                               "cohorts": {"private": private, "iot": iot}}
    conductor._ping.return_value = {"success": traffic}
    observer = Mock()
    observer.last_raw = {"topology": {}}
    observer.observe.return_value.health = Mock(devices=5, clients=clients)
    with patch("room_demo.conductor.ControllerObserver", return_value=observer):
        LiveConductor.preflight(conductor)


@pytest.mark.parametrize("band", ["2.4", "5", "6"])
def test_interactive_restart_accepts_a_valid_native_band_after_room_restoration(band):
    preflight(band, True)


def test_interactive_full_pool_preflight_precedes_default_room_isolation():
    preflight("5", True, capacity=100, clients=100, private=50, iot=50)


@pytest.mark.parametrize("options", [{"clients": 99}, {"private": 49}, {"iot": 49}])
def test_full_pool_preflight_still_rejects_incomplete_or_wrong_cohorts(options):
    with pytest.raises(RuntimeError, match="demo preflight failed"):
        preflight("5", True, capacity=100, **{"clients": 100, "private": 50, "iot": 50, **options})


@pytest.mark.parametrize("capacity", [0, 99, 102])
def test_pool_manifest_rejects_invalid_capacity(capacity):
    with pytest.raises(ValueError, match="balanced private/IoT"):
        preflight("5", True, capacity=capacity)


@pytest.mark.parametrize("band", ["2.4", "6", None, "unknown"])
def test_scripted_five_ghz_manifest_still_requires_five_ghz(band):
    with pytest.raises(RuntimeError, match="hero band"):
        preflight(band, False)


@pytest.mark.parametrize("options", [{"band": None}, {"band": "unknown"}, {"traffic": False},
                                    {"clients": 19}, {"ssid": "foreign"}, {"rcpi": None}])
def test_interactive_band_support_does_not_bypass_identity_health_or_traffic(options):
    with pytest.raises(RuntimeError, match="demo preflight failed"):
        preflight(interactive=True, **{"band": "5", **options})
