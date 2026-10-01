from __future__ import annotations

import copy

from wmdcfg.model import ScenarioError


CLIENT_CAPACITY = 100


def pool_manifest(manifest: dict, plan: dict) -> dict:
    clients = sum(binding["role_type"] == "station" for binding in plan["bindings"].values())
    if not 0 < clients <= CLIENT_CAPACITY or clients % 2:
        raise ScenarioError("the lab pool requires balanced private/IoT cohorts within 100 clients")
    result = copy.deepcopy(manifest)
    result["health"].update(expected_clients=clients, expected_private_clients=clients // 2,
                            expected_iot_clients=clients // 2)
    return result


def _client_order(container: str) -> tuple[str, int]:
    prefix, _, suffix = container.rpartition("-")
    return (prefix, int(suffix)) if suffix.isdigit() else (container, 0)


def bind_client_pool(world: dict, binding_doc: dict, inventory: dict) -> dict:
    result = copy.deepcopy(binding_doc)
    bindings = result["roles"]
    stations = sorted((item["container"] for item in inventory.get("radios", [])
                       if item.get("kind") == "station"), key=_client_order)
    if len(stations) > CLIENT_CAPACITY or len(stations) != len(set(stations)):
        raise ScenarioError("the room requires unique client identities within the 100-client capacity")
    remaining = [container for container in stations if container not in bindings.values()]
    for ordinal, container in enumerate(remaining, 21):
        role = f"sta_pool_{ordinal:03d}"
        if role in bindings:
            raise ScenarioError(f"pool role is already bound: {role}")
        bindings[role] = container
    missing = set(world["roles"]) - set(bindings)
    if missing:
        raise ScenarioError(f"world exceeds the provisioned client pool: {sorted(missing)}")
    return result


def expand_initial_world(world: dict, roles: dict) -> dict:
    result = copy.deepcopy(world)
    result["roles"] = dict(roles)
    first = result["generations"][0]
    for role in roles:
        first["positions"].setdefault(role, [0, 0])
        first["present"].setdefault(role, False)
    result["generations"] = [first]
    return result
