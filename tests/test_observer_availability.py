from unittest.mock import Mock

import pytest

from optimizer.observer import ControllerInventoryUnavailable, ControllerObserver


@pytest.mark.parametrize("endpoint,field", [
    ("topology", "nodes"), ("clients", "clients"),
    ("devices", "devices"), ("bsses", "bsses"),
])
@pytest.mark.parametrize("rows", [None, {}, "not an array", [None], [False]])
def test_unavailable_native_inventory_is_recoverable(endpoint, field, rows):
    fetcher = Mock(return_value={field: rows})
    observer = ControllerObserver("http://controller", fetcher=fetcher)
    with pytest.raises(ControllerInventoryUnavailable, match="unavailable or malformed"):
        observer._get("/api/v1/" + endpoint)
    fetcher.return_value = {field: []}
    assert observer._get("/api/v1/" + endpoint) == {field: []}


@pytest.mark.parametrize("payload", [None, [], "unavailable"])
def test_invalid_top_level_payload_is_not_a_fatal_type_error(payload):
    observer = ControllerObserver("http://controller", fetcher=Mock(return_value=payload))
    with pytest.raises(ControllerInventoryUnavailable, match="response unavailable"):
        observer.observe()
