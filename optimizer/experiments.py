from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any


class ExperimentError(ValueError):
    pass


def _normalize_numbers(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_normalize_numbers(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_numbers(item) for key, item in value.items()}
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(
        _normalize_numbers(value), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _load(path: Path, schema: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ExperimentError(f"{path}: {error}") from error
    if value.get("schema") != schema:
        raise ExperimentError(f"{path}: expected schema {schema}")
    return value


def _relative_path(base: Path, value: str) -> Path:
    result = (base / value).resolve()
    if not result.is_file():
        raise ExperimentError(f"referenced file does not exist: {value}")
    return result


def _ids(values: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    result = {}
    for item in values:
        identifier = item.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", identifier):
            raise ExperimentError(f"{label} has invalid id {identifier!r}")
        if identifier in result:
            raise ExperimentError(f"duplicate {label} id {identifier}")
        result[identifier] = item
    return result


def _verify_world(path: Path) -> dict[str, Any]:
    world = _load(path, "wmdcfg.world-plan.v1")
    claimed = world.get("golden_sha256")
    unsigned = dict(world)
    unsigned.pop("golden_sha256", None)
    if claimed != _hash(unsigned):
        raise ExperimentError(f"{path}: invalid golden_sha256")
    return world


def _matches(world: dict[str, Any], scenario: dict[str, Any]) -> bool:
    tags = set(world.get("tags", []))
    required = set(scenario.get("world_tags_all", []))
    alternatives = set(scenario.get("world_tags_any", []))
    excluded = set(scenario.get("world_tags_none", []))
    return required <= tags and (not alternatives or bool(tags & alternatives)) and not (tags & excluded)


def _scale_requirement(world: dict[str, Any]) -> str:
    counts = world.get("counts", {})
    agents = int(counts.get("agents", 0))
    stations = int(counts.get("stations", 0))
    if agents <= 5 and stations <= 20:
        return "scale-profile-small"
    if agents <= 5 and stations <= 50:
        return "scale-profile-medium"
    return "scale-profile-stress"


def build_matrix(spec_path: str | Path) -> dict[str, Any]:
    """Expand the checked-in RF, traffic, policy and seed axes deterministically.

    Golden wmediumd values remain experiment truth. This code reads only their
    identity, tags and scale; it never turns them into optimizer observations.
    """
    spec_path = Path(spec_path).resolve()
    base = spec_path.parent
    spec = _load(spec_path, "optimizer.experiment-suite.v1")
    capabilities_path = _relative_path(base, spec["capabilities"])
    traffic_path = _relative_path(base, spec["traffic_suite"])
    catalog_path = _relative_path(base, spec["scenario_catalog"])
    capabilities_doc = _load(capabilities_path, "optimizer.capabilities.v1")
    traffic_doc = _load(traffic_path, "optimizer.traffic-suite.v1")
    catalog_doc = _load(catalog_path, "optimizer.scenario-catalog.v1")

    capability_entries = capabilities_doc.get("capabilities", {})
    if not isinstance(capability_entries, dict):
        raise ExperimentError("capabilities must be an object")
    available = {
        name for name, entry in capability_entries.items()
        if isinstance(entry, dict) and entry.get("available") is True
    }
    traffic = _ids(traffic_doc.get("profiles", []), "traffic profile")
    scenarios = _ids(catalog_doc.get("scenarios", []), "scenario")
    worlds = []
    for configured in spec.get("worlds", []):
        path = _relative_path(base, configured)
        worlds.append((configured, _verify_world(path)))
    if not worlds:
        raise ExperimentError("suite defines no world plans")

    policies = []
    for configured in spec.get("policies", []):
        path = _relative_path(base, configured)
        policies.append(
            {
                "id": path.stem,
                "path": configured,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    if not policies:
        raise ExperimentError("suite defines no policies")
    seeds = spec.get("seeds", [])
    if not seeds or any(not isinstance(seed, int) or seed < 0 for seed in seeds):
        raise ExperimentError("suite seeds must be non-negative integers")

    cases = []
    coverage = {}
    for scenario_id, scenario in sorted(scenarios.items()):
        matching = [(path, world) for path, world in worlds if _matches(world, scenario)]
        if not matching:
            raise ExperimentError(f"scenario {scenario_id} matches no world")
        profile_ids = scenario.get("traffic_profiles", [])
        if not profile_ids:
            raise ExperimentError(f"scenario {scenario_id} selects no traffic profile")
        unknown = sorted(set(profile_ids) - set(traffic))
        if unknown:
            raise ExperimentError(f"scenario {scenario_id} has unknown traffic profiles: {unknown}")
        scenario_status = {"runnable": 0, "blocked": 0}
        for world_path, world in matching:
            for profile_id in profile_ids:
                profile = traffic[profile_id]
                for policy in policies:
                    for seed in seeds:
                        requirements = sorted(
                            set(scenario.get("requirements", []))
                            | set(profile.get("requirements", []))
                            | {_scale_requirement(world)}
                        )
                        missing = sorted(set(requirements) - available)
                        status = "blocked" if missing else "runnable"
                        scenario_status[status] += 1
                        identifier = "--".join(
                            (
                                scenario_id,
                                world["name"],
                                profile_id,
                                policy["id"],
                                f"seed-{seed}",
                            )
                        )
                        cases.append(
                            {
                                "id": identifier,
                                "scenario": scenario_id,
                                "scenario_class": scenario["class"],
                                "world": {
                                    "path": world_path,
                                    "name": world["name"],
                                    "golden_sha256": world["golden_sha256"],
                                    "layout": world["layout"],
                                    "mobility": world["mobility"],
                                    "tags": world.get("tags", []),
                                    "counts": world["counts"],
                                    "duration_ms": world["duration_ms"],
                                    "station_roles": sorted(
                                        role for role, kind in world["roles"].items()
                                        if kind == "station"
                                    ),
                                },
                                "traffic": {
                                    "id": profile_id,
                                    "driver": profile["driver"],
                                    "schedule": profile.get("schedule", []),
                                },
                                "policy": policy,
                                "seed": seed,
                                "requirements": requirements,
                                "missing_capabilities": missing,
                                "status": status,
                                "expected_behavior": scenario.get("expected_behavior", []),
                                "truth_boundary": {
                                    "optimizer_inputs": "EasyMesh observations only",
                                    "evaluator_only": ["world golden RF", "traffic schedule"],
                                },
                            }
                        )
        coverage[scenario_id] = scenario_status

    cases.sort(key=lambda item: item["id"])
    result = {
        "schema": "optimizer.experiment-matrix.v1",
        "suite": spec["name"],
        "source": {
            "spec": spec_path.name,
            "spec_sha256": hashlib.sha256(spec_path.read_bytes()).hexdigest(),
            "capabilities": spec["capabilities"],
            "capabilities_sha256": hashlib.sha256(capabilities_path.read_bytes()).hexdigest(),
            "traffic_suite": spec["traffic_suite"],
            "traffic_suite_sha256": hashlib.sha256(traffic_path.read_bytes()).hexdigest(),
            "scenario_catalog": spec["scenario_catalog"],
            "scenario_catalog_sha256": hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
        },
        "summary": {
            "cases": len(cases),
            "runnable": sum(item["status"] == "runnable" for item in cases),
            "blocked": sum(item["status"] == "blocked" for item in cases),
            "worlds": len(worlds),
            "traffic_profiles": len(traffic),
            "policies": len(policies),
            "seeds": len(seeds),
        },
        "coverage": coverage,
        "cases": cases,
    }
    result["matrix_sha256"] = _hash(result)
    return result


def verify_matrix(matrix: dict[str, Any]) -> None:
    if matrix.get("schema") != "optimizer.experiment-matrix.v1":
        raise ExperimentError("matrix schema must be optimizer.experiment-matrix.v1")
    claimed = matrix.get("matrix_sha256")
    unsigned = dict(matrix)
    unsigned.pop("matrix_sha256", None)
    if claimed != _hash(unsigned):
        raise ExperimentError("matrix_sha256 does not match its contents")
