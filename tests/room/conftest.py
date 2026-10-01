"""The room service's tests. They share fakes by module name (test_interactions'
FakeClient and its plan), so their directory is importable; they find the medium
where the labs mount it, next to this checkout (``../medium``). They are written
against the RDK stack, in either lab: a test of another stack names it."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def _rdk_stack(monkeypatch):
    monkeypatch.setenv("OPTIMIZER_STACK", "rdk")
