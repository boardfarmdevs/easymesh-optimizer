"""The lab's standard rooms: its own rooms with the extender on a wired backhaul
(extender_5, worlds-wired), selected by its manifest alone, the rooms they extend
unchanged. With the OpenSync pods as well: test_pod_worlds.py."""
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
NATIVE_WIRED = CONFIGURATOR / "worlds-wired"
NATIVE_WIRED_MANIFEST = REPO / "gen/demo/manifests/private-client-room-walk-wired.json"
# the rooms about the wired extender itself (in the pod rooms too)
WIRED_ONLY = {"home-a-wired-walk-in", "home-a-wired-walk-out", "home-a-wired-extender-loss-recovery",
              "backhaul-wired-parent"}


def mirrored(root):
    return [path for path in sorted((root / "golden").glob("*.world.json"))
            if path.name.removesuffix(".world.json") not in WIRED_ONLY]


def catalog(root):
    world = load_json(root / "golden/home-a-private-client-room-walk.world.json")
    return BoundWorlds(world, load_json(root / "layouts" / f"{world['layout']}.json"), root).catalog()


class NativeWiredExtenderWorldTests(unittest.TestCase):
    def test_the_manifest_names_its_world_bindings_and_worlds_root(self):
        manifest = load_manifest(NATIVE_WIRED_MANIFEST, REPO)
        self.assertEqual(REPO / manifest["worlds_root"], NATIVE_WIRED)
        world = load_json(REPO / manifest["world"])
        bindings = json.loads((REPO / manifest["bindings"]).read_text())["roles"]
        self.assertTrue(set(world["roles"]) <= set(bindings))
        self.assertEqual(bindings["extender_5"], "bpiap-004")
        self.assertEqual(world["wired_backhaul"], ["extender_5"])
        self.assertFalse([role for role in bindings if role.startswith("pod_")])

    def test_every_room_is_the_native_room_plus_the_wired_extender(self):
        for path in mirrored(NATIVE_WIRED):
            wired, native = load_json(path), load_json(NATIVE / "golden" / path.name)
            self.assertEqual(set(wired["roles"]) - set(native["roles"]), {"extender_5"}, path.name)
            self.assertEqual(wired["layout"], native["layout"] + "-wired", path.name)
            self.assertEqual(wired["mobility"], native["mobility"], path.name)

    def test_a_room_bound_with_it_offers_the_native_rooms_and_its_own(self):
        wired, native = catalog(NATIVE_WIRED), catalog(NATIVE)
        self.assertEqual(sorted(entry["id"] for entry in wired["worlds"]),
                         sorted([entry["id"] for entry in native["worlds"]] + list(WIRED_ONLY)))
        geometry = {entry["id"] for entry in wired["worlds"] if entry["backhaul_rf"] == "geometry"}
        self.assertIn("backhaul-wired-parent", geometry)
        self.assertEqual(wired["mesh_devices"], native["mesh_devices"] + 1)

    def test_the_catalog_names_its_tree_for_the_suite(self):
        self.assertEqual(catalog(NATIVE_WIRED)["worlds_root"], "gen/medium/configurator/worlds-wired")
        self.assertEqual(catalog(NATIVE)["worlds_root"], "gen/medium/configurator/worlds")

    def test_its_own_rooms_expect_clients_on_it(self):
        for name in ("home-a-wired-walk-in", "home-a-wired-extender-loss-recovery"):
            world = load_json(NATIVE_WIRED / "golden" / f"{name}.world.json")
            final = next(item for item in world["ap_expectations"] if item["at"] == "final")
            self.assertEqual(set(final["roles"].values()), {"extender_5"}, name)
        out = load_json(NATIVE_WIRED / "golden/home-a-wired-walk-out.world.json")
        self.assertEqual([item["at"] for item in out["ap_expectations"]], [3000, "final"])
        loss = load_json(NATIVE_WIRED / "golden/home-a-wired-extender-loss-recovery.world.json")
        self.assertEqual([generation["present"]["extender_5"] for generation in loss["generations"]
                          if generation["time_ms"] in (10000, 40000, 70000)], [True, False, True])

    def test_the_goldens_match_their_layouts_and_positions(self):
        result = subprocess.run([sys.executable, str(NATIVE_WIRED / "build-goldens.py"), "--check"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
