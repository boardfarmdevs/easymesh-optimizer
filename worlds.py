from __future__ import annotations

import copy
import math
from pathlib import Path
import re
from typing import Any

from wmdcfg.world import _hash, _merge_nodes, _validate_layout, load_json, verify_world_plan

from .interactions import InteractionError


class BoundWorlds:
    """Select geometry and an online subset of an immutable, already bound lab."""

    def __init__(self, world: dict, layout: dict, root: Path):
        self.default_world = copy.deepcopy(world)
        self.default_layout = copy.deepcopy(layout)
        self.root = root
        self.roles = dict(world["roles"])
        self.mesh_roles = {
            role for role, kind in self.roles.items() if kind == "fronthaul_ap"
        }

    def rf_nodes(self, world: dict, layout: dict) -> dict:
        if "rf_nodes" in world:
            metadata = world["rf_nodes"]
            if not isinstance(metadata, dict) or set(metadata) != set(world["roles"]):
                raise ValueError("RF metadata must cover exactly the world roles")
            nodes = {node["role"]: dict(node) for node in layout.get("nodes", [])}
            for role, value in metadata.items():
                if not isinstance(value, dict) or set(value) - {"tx_gain_db_by_band"}:
                    raise ValueError("unsupported RF node metadata")
                gains = value.get("tx_gain_db_by_band", {})
                if not isinstance(gains, dict) or set(gains) - {"2.4", "5", "6"}:
                    raise ValueError("invalid RF gain bands")
                if any(type(gain) not in (int, float) or not math.isfinite(gain)
                       for gain in gains.values()):
                    raise ValueError("RF gains must be finite numbers")
                nodes.setdefault(role, {"role": role}).update(copy.deepcopy(value))
            return {role: {"role": role, **nodes.get(role, {})} for role in self.roles}
        name = world.get("mobility")
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", name):
            raise ValueError("invalid mobility name")
        mobility = load_json(self.root / "mobility" / f"{name}.json")
        if _hash(mobility) != world.get("mobility_sha256"):
            raise ValueError("world does not match its installed mobility")
        nodes = {node["role"]: node for node in _merge_nodes(layout, mobility)}
        return {role: {"role": role, **nodes.get(role, {})} for role in self.roles}

    def select(self, selection: Any) -> tuple[dict, dict]:
        try:
            if selection == "default":
                return copy.deepcopy(self.default_world), copy.deepcopy(self.default_layout)
            if isinstance(selection, str):
                if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", selection):
                    raise ValueError("invalid catalog world name")
                world = load_json(self.root / "golden" / f"{selection}.world.json")
            elif isinstance(selection, dict):
                world = copy.deepcopy(selection)
            else:
                raise ValueError("select a catalog name or supply a world document")
            verify_world_plan(world)
            if not isinstance(world.get("name"), str) or not 1 <= len(world["name"]) <= 200:
                raise ValueError("world requires a name of 1..200 characters")
            name = world.get("layout")
            if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", name):
                raise ValueError("invalid layout name")
            layout = load_json(self.root / "layouts" / f"{name}.json")
            _validate_layout(layout)
            if _hash(layout) != world.get("layout_sha256"):
                raise ValueError("world does not match its installed layout")
            self.rf_nodes(world, layout)
            roles = world["roles"]
            if not isinstance(roles, dict) or not roles:
                raise ValueError("world requires bound roles")
            for role, kind in roles.items():
                if self.roles.get(role) != kind:
                    raise ValueError(f"role {role!r} is not bound with kind {kind!r} in this lab")
            mesh = {role for role, kind in roles.items() if kind == "fronthaul_ap"}
            if mesh != self.mesh_roles:
                raise ValueError("world must retain every existing mesh role")
            if not any(kind == "station" for kind in roles.values()):
                raise ValueError("world requires at least one bound client")
            if world.get("bands") != ["2.4", "5", "6"]:
                raise ValueError("world must retain the existing 2.4/5/6 GHz bands")
            first = world["generations"][0]
            if first["time_ms"] != 0:
                raise ValueError("world must start at time zero")
            expected_links = {
                (source, destination) for source in roles for destination in roles
                if source != destination and "fronthaul_ap" in (roles[source], roles[destination])
            }
            links = first.get("links")
            if not isinstance(links, list) or len(links) != len(expected_links):
                raise ValueError("initial links must cover every directed AP/client and mesh-peer pair")
            actual_links = set()
            for link in links:
                pair = (link["source_role"], link["destination_role"])
                if pair not in expected_links or pair in actual_links:
                    raise ValueError("initial links contain an unknown or duplicate pair")
                actual_links.add(pair)
                expected_class = "backhaul" if all(roles[role] == "fronthaul_ap" for role in pair) else "fronthaul"
                if link.get("link_class") != expected_class or set(link["snr_db_by_band"]) != {"2.4", "5", "6"}:
                    raise ValueError("initial link class or bands are invalid")
                for value in link["snr_db_by_band"].values():
                    if type(value) not in (int, float) or not math.isfinite(value) or not -20 <= value <= 60:
                        raise ValueError("initial link SNR must be finite and inside -20..60 dB")
                for field in ("distance_m", "wall_loss_db"):
                    value = link[field]
                    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                        raise ValueError(f"initial link {field} must be finite and nonnegative")
            for field in ("positions", "present"):
                if set(first[field]) != set(roles):
                    raise ValueError(f"initial {field} must cover exactly the world roles")
            for role in roles:
                position = first["positions"][role]
                if not isinstance(position, list) or len(position) != 2:
                    raise ValueError(f"role {role!r} requires a two-dimensional position")
                for value, maximum in zip(position, (layout["space"]["width_m"], layout["space"]["height_m"])):
                    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
                        raise ValueError(f"role {role!r} position is outside the room")
                if type(first["present"][role]) is not bool:
                    raise ValueError(f"role {role!r} presence must be boolean")
            if first["present"].get("gateway") is not True:
                raise ValueError("Agent-1/gateway must remain present")
            if not any(first["present"][role] for role, kind in roles.items() if kind == "station"):
                raise ValueError("world requires at least one initially online client")
            if type(world.get("duration_ms")) is not int or world["duration_ms"] <= 0:
                raise ValueError("world duration must be positive")
            if type(world.get("tick_ms")) is not int or not 100 <= world["tick_ms"] <= 60000:
                raise ValueError("world tick must be 100..60000 milliseconds")
            world["walls"] = copy.deepcopy(layout.get("walls", []))
            world["counts"] = {**world.get("counts", {}), "agents": len(mesh),
                               "stations": len(roles) - len(mesh)}
            return world, layout
        except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError) as error:
            raise InteractionError(422, "unsupported_world", str(error)) from error

    def catalog(self) -> dict:
        entries = []
        for path in sorted((self.root / "golden").glob("*.world.json")):
            name = path.name.removesuffix(".world.json")
            try:
                world, _ = self.select(name)
                entries.append({"id": name, "name": world["name"],
                                "clients": sum(kind == "station" for kind in world["roles"].values())})
            except InteractionError:
                continue
        return {"default": "default", "clients": len(self.roles) - len(self.mesh_roles),
                "mesh_devices": len(self.mesh_roles), "worlds": entries}
