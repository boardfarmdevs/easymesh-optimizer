from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location("rf_access_smoke", Path(__file__).with_name("rf-access-smoke.py"))
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def fixture():
    identity = {"frequency_mhz": 5180, "bssid": "02:00:00:00:01:01"}
    load = {"property": "native_utilization", "state": "valid", "value": 0, "identity": identity,
            "source": "native_ap_metrics", "observed_at": "2026-09-21T12:00:00Z", "maximum_age_seconds": 5}
    inspection = {"schema": "easymesh.rf-inspection.v2", "enabled": True,
                  "observations": {"schema": "easymesh.rf-observations.v1", "decision_inputs": False,
                                   "records": [deepcopy(load)]},
                  "backhaul": {"schema": "easymesh.backhaul-observations.v1", "decision_inputs": False,
                               "paths": [{"role": "extender_1", "state": "valid",
                                          "links": [{**identity, "wireless": True, "utilization": load}]}]}}
    return ({"schema": "easymesh.rf-properties.v1"}, inspection,
            {"schema": "easymesh.room-layout.v1", "rf_observations": {"enabled": True}})


def test_zero_backhaul_load_is_valid():
    assert not MODULE.validate(*fixture(), now=datetime(2026, 9, 21, 12, tzinfo=timezone.utc).timestamp(),
                               require_backhaul_load=True)


@pytest.mark.parametrize("field,value", [("state", "stale"), ("value", None), ("value", True),
                                        ("source", "geometry"),
                                        ("observed_at", "2026-09-21T11:59:00Z"),
                                        ("observed_at", "2026-09-21T12:01:00Z"),
                                        ("observed_at", None), ("observed_at", 42),
                                        ("identity", {"frequency_mhz": 5200, "bssid": "02:00:00:00:01:01"})])
def test_strict_backhaul_check_cannot_pass_on_general_fronthaul_load(field, value):
    catalog, inspection, layout = fixture()
    inspection["backhaul"]["paths"][0]["links"][0]["utilization"][field] = value
    arguments = dict(now=datetime(2026, 9, 21, 12, tzinfo=timezone.utc).timestamp())
    assert not MODULE.validate(catalog, inspection, layout, **arguments)
    assert MODULE.validate(catalog, inspection, layout, require_backhaul_load=True, **arguments)


def test_strict_backhaul_check_requires_paths():
    catalog, inspection, layout = fixture()
    inspection["backhaul"]["paths"] = []
    assert "no native backhaul paths" in MODULE.validate(
        catalog, inspection, layout, now=datetime(2026, 9, 21, 12, tzinfo=timezone.utc).timestamp(),
        require_backhaul_load=True)


@pytest.mark.parametrize("timestamp", [None, 42])
def test_invalid_record_timestamp_is_reported(timestamp):
    catalog, inspection, layout = fixture()
    inspection["observations"]["records"][0]["observed_at"] = timestamp
    assert "valid record lacks freshness identity" in MODULE.validate(
        catalog, inspection, layout, now=datetime(2026, 9, 21, 12, tzinfo=timezone.utc).timestamp())
