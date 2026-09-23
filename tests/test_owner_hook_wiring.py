from unittest.mock import Mock

import pytest

from optimizer import cli
from .helpers import snapshot


BACKENDS = ["rdk"] + (["prplmesh"] if hasattr(cli, "PrplMeshObserver") else [])


@pytest.mark.parametrize("backend", BACKENDS)
def test_live_collector_registers_owner_observer_without_another_read(backend, tmp_path, monkeypatch):
    value = snapshot(0)
    raw = {"topology": {}}
    receiver = Mock()
    receiver.enrich.side_effect = lambda snapshot, raw: snapshot
    observed = []

    class Observer:
        def __init__(self, *arguments, ownership_observer=None, **keywords):
            self.ownership_observer = ownership_observer
            self.last_raw = raw

        def observe(self):
            observed.append("read")
            if self.ownership_observer:
                self.ownership_observer(value.clients, raw)
            return value

    monkeypatch.setattr(cli, "ControllerObserver" if backend == "rdk" else "PrplMeshObserver", Observer)
    arguments = ["observe", "--journal", str(tmp_path / "journal.jsonl"), "--count", "1"]
    if hasattr(cli, "PrplMeshObserver"):
        arguments += ["--backend", backend]
    args = cli.parser().parse_args(arguments)
    assert cli._live_run(args, "observe", receiver) == 0
    receiver.observe_owners.assert_called_once_with(value.clients, raw)
    receiver.enrich.assert_called_once_with(value, raw)
    assert observed == ["read"]
