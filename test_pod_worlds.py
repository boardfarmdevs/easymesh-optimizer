"""The room variant with two OpenSync pods (EMOSA adapter): the lab's standard rooms
(with extender_5 on a wired backhaul) plus the pods, selected by its manifest alone,
with the lab's own rooms unchanged."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
import unittest

from room_demo.conductor import load_manifest
from room_demo.worlds import BoundWorlds
from wmdcfg.world import load_json

REPO = Path(__file__).resolve().parents[3]
CONFIGURATOR = REPO / "gen/medium/configurator"
NATIVE = CONFIGURATOR / "worlds"
PODS = CONFIGURATOR / "worlds-pods"
STANDARD = CONFIGURATOR / "worlds-wired"
MANIFEST = REPO / "gen/demo/manifests/private-client-room-walk-pods.json"


def _bound(root: Path, world_path: Path) -> BoundWorlds:
    world = load_json(world_path)
    layout = load_json(root / "layouts" / f"{world['layout']}.json")
    return BoundWorlds(world, layout, root)


class PodWorldTests(unittest.TestCase):
    def test_the_pod_manifest_names_its_world_bindings_and_worlds_root(self):
        manifest = load_manifest(MANIFEST, REPO)
        self.assertEqual(REPO / manifest["worlds_root"], PODS)
        world = load_json(REPO / manifest["world"])
        bindings = json.loads((REPO / manifest["bindings"]).read_text())["roles"]
        self.assertTrue(set(world["roles"]) <= set(bindings))
        self.assertEqual((bindings["pod_1"], bindings["pod_2"]), ("pod-1", "pod-2"))
        self.assertEqual(bindings["extender_5"], "bpiap-004")
        self.assertEqual(world["roles"]["pod_1"], "fronthaul_ap")
        self.assertEqual(world["wired_backhaul"], ["extender_5"])

    def test_a_room_bound_with_pods_offers_every_standard_room_with_pods(self):
        worlds = _bound(PODS, PODS / "golden/home-a-private-client-room-walk.world.json")
        catalog = worlds.catalog()
        standard = _bound(STANDARD, STANDARD / "golden/home-a-private-client-room-walk.world.json").catalog()
        # the same rooms as the lab's standard ones, under the same IDs
        self.assertEqual([entry["id"] for entry in catalog["worlds"]],
                         [entry["id"] for entry in standard["worlds"]])
        self.assertEqual(catalog["mesh_devices"], standard["mesh_devices"] + 2)
        self.assertEqual(catalog["worlds_root"], "gen/medium/configurator/worlds-pods")
        world, layout = worlds.select("home-a-stationary")
        self.assertEqual(layout["name"], "home-five-agent-pods")
        self.assertEqual({role for role, kind in world["roles"].items() if kind == "fronthaul_ap"},
                         {"gateway", "extender_1", "extender_2", "extender_3", "extender_4", "extender_5",
                          "pod_1", "pod_2"})

    def test_every_pod_room_is_the_standard_room_plus_the_pods(self):
        for path in sorted((PODS / "golden").glob("*.world.json")):
            pods, standard = load_json(path), load_json(STANDARD / "golden" / path.name)
            self.assertEqual(set(pods["roles"]) - set(standard["roles"]), {"pod_1", "pod_2"}, path.name)
            self.assertEqual(pods["wired_backhaul"], ["extender_5"], path.name)
            self.assertEqual(pods["mobility"], standard["mobility"], path.name)
            for index, (a, b) in enumerate(zip(pods["generations"], standard["generations"])):
                self.assertEqual(a["positions"]["extender_5"], b["positions"]["extender_5"], f"{path.name} {index}")

    def test_the_lab_rooms_are_unchanged(self):
        worlds = _bound(NATIVE, NATIVE / "golden/home-a-private-client-room-walk.world.json")
        catalog = worlds.catalog()
        self.assertEqual(catalog["mesh_devices"], 5)
        # none of the native rooms carries a pod
        self.assertTrue(catalog["worlds"])
        for entry in catalog["worlds"]:
            world, _ = worlds.select(entry["id"])
            self.assertNotIn("pod_1", world["roles"])

    def test_pod_rooms_are_not_stale(self):
        result = subprocess.run([sys.executable, str(PODS / "build-goldens.py"), "--check"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
