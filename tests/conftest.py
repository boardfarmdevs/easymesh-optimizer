from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
CONFIGURATOR = ROOT.parent / "wmediumd" / "configurator"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CONFIGURATOR))


def pytest_configure(config):
    config.addinivalue_line("markers", "scenario: test coupled to a wmdcfg scenario contract")
