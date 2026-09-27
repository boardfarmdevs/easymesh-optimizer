"""The room variant with the two OpenSync pods and the lab's extender on a wired
backhaul (extender_5): selected by its manifest alone, with the pod rooms
unchanged."""
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
CONFIGURATOR = REPO / "gen/wmediumd/configurator"
PODS = CONFIGURATOR / "worlds-pods"
WIRED = CONFIGURATOR / "worlds-pods-wired"
MANIFEST = REPO / "gen/demo/manifests/private-client-room-walk-pods-wired.json"


class WiredExtenderWorldTests(unittest.TestCase):
    def test_the_manifest_names_its_world_bindings_and_worlds_root(self):
        manifest = load_manifest(MANIFEST, REPO)
        self.assertEqual(REPO / manifest["worlds_root"], WIRED)
        world = load_json(REPO / manifest["world"])
        bindings = json.loads((REPO / manifest["bindings"]).read_text())["roles"]
        self.assertTrue(set(world["roles"]) <= set(bindings))
        self.assertEqual(bindings["extender_5"], "bpiap-004")
        self.assertEqual(world["wired_backhaul"], ["extender_5"])

    def test_every_room_is_the_pod_room_plus_a_wired_extender_without_backhaul_links(self):
        for path in sorted((WIRED / "golden").glob("*.world.json")):
            wired, pod = load_json(path), load_json(PODS / "golden" / path.name)
            self.assertEqual(set(wired["roles"]) - set(pod["roles"]), {"extender_5"}, path.name)
            self.assertEqual(wired["wired_backhaul"], ["extender_5"])
            for generation in wired["generations"]:
                self.assertFalse([link for link in generation["links"] if link["link_class"] == "backhaul"
                                  and "extender_5" in (link["source_role"], link["destination_role"])], path.name)

    def test_a_room_bound_with_the_wired_extender_offers_the_pod_rooms(self):
        def catalog(root):
            world = load_json(root / "golden/home-a-private-client-room-walk.world.json")
            return BoundWorlds(world, load_json(root / "layouts" / f"{world['layout']}.json"), root).catalog()
        wired, pods = catalog(WIRED), catalog(PODS)
        self.assertEqual([entry["id"] for entry in wired["worlds"]], [entry["id"] for entry in pods["worlds"]])
        self.assertEqual(wired["mesh_devices"], pods["mesh_devices"] + 1)

    def test_the_goldens_match_their_layouts_and_positions(self):
        result = subprocess.run([sys.executable, str(WIRED / "build-goldens.py"), "--check"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
