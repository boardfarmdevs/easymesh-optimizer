from __future__ import annotations

from pathlib import Path
from typing import Any

from .policy import PolicyConfig


def _scalar(value: str) -> Any:
    value = value.strip()
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value.strip("'\"")


def load_policy(path: str | Path) -> PolicyConfig:
    """Load the flat, dependency-free YAML subset used by optimizer policies."""
    values: dict[str, Any] = {}
    for number, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            raise ValueError(f"{path}:{number}: expected key: value")
        key, value = line.split(":", 1)
        key = key.strip()
        if not key or not value.strip():
            raise ValueError(f"{path}:{number}: expected scalar key: value")
        if key in values:
            raise ValueError(f"{path}:{number}: duplicate key {key}")
        values[key] = _scalar(value)
    return PolicyConfig(**values)
