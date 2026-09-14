from __future__ import annotations

import pytest

from optimizer.model import Snapshot, normalize_band, normalize_mac
from .helpers import snapshot


def test_snapshot_round_trip_is_lossless():
    original = snapshot(5)
    assert Snapshot.from_dict(original.to_dict()) == original


def test_mac_normalization_rejects_non_mac_input():
    assert normalize_mac("02:00:00:AA:BB:01") == "02:00:00:aa:bb:01"
    with pytest.raises(ValueError, match="invalid MAC"):
        normalize_mac("extender-1")


def test_easy_mesh_band_values_are_normalized():
    assert normalize_band(0) == "2.4"
    assert normalize_band(1) == "5"
    assert normalize_band(3) == "6"
    assert normalize_band("6GHz") == "6"
    assert normalize_band(None) is None
    with pytest.raises(ValueError, match="invalid Wi-Fi band"):
        normalize_band("60GHz")
