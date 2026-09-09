from __future__ import annotations

import unittest
from pathlib import Path


VIEWER = (
    Path(__file__).resolve().parents[2]
    / "wmediumd/configurator/worlds/viewer/index.html"
)


class ViewerTopologyContractTests(unittest.TestCase):
    def test_world_load_applies_immediately_and_observers_rehydrate_authoritative_roles(self):
        source = VIEWER.read_text(encoding="utf-8")
        self.assertNotIn('id="applyWorld"', source)
        self.assertNotIn("pendingWorld", source)
        self.assertNotIn("window.confirm('Apply this world", source)
        self.assertIn('id="defaultWorld"', source)
        self.assertIn("'/api/demo/world/apply'", source)
        self.assertIn("event.kind === 'room.world.committed'", source)
        self.assertIn("Object.entries(state.roles || {})", source)
        self.assertIn("value.authoritative_position.slice()", source)
        self.assertIn("if (liveMode)", source)
        self.assertIn("return applySelectedWorld(sel.value)", source)
        self.assertIn("return applySelectedWorld(file)", source)
        self.assertIn("setWorldControlsBusy(applyingWorld)", source)
        self.assertIn("selectCurrentLiveWorld();", source)
        self.assertIn("Startup mesh backhaul is protected", source)
        self.assertIn("Client roster ready; see optimizer for AP convergence.", source)

    def test_signal_meters_share_the_topology_palette(self):
        source = VIEWER.read_text(encoding="utf-8")
        self.assertIn('src="signal-meter.js?v=signal-meter-1"', source)
        self.assertIn("signalMeter.segmentColor(index, level)", source)
        self.assertIn("signalMeter.rssiLevel(observed.rssi_dbm)", source)
        self.assertIn("signalMeter.snrLevel(predicted && predicted.snr)", source)
        self.assertIn("referenceTime - measuredAt > 20000", source)
        self.assertNotIn("Private-Laptop", source)

    def test_live_backhaul_uses_controller_graph_not_rf_possibilities(self):
        source = VIEWER.read_text(encoding="utf-8")

        self.assertIn("mesh.backhaul_edges || []", source)
        self.assertIn("edge.parent_role", source)
        self.assertIn("edge.child_role", source)
        self.assertIn("else if (!liveMode && !replayMode)", source)
        self.assertIn("Actual mesh backhaul", source)

    def test_live_ap_labels_use_the_same_controller_identity(self):
        source = VIEWER.read_text(encoding="utf-8")

        self.assertIn("const meshNode = observedMeshNode(role);", source)
        self.assertIn("meshNode && meshNode.name", source)
        self.assertIn("actualBackhaulFor(selected)", source)

    def test_static_no_connect_mode_is_an_explicit_interactive_sandbox(self):
        source = VIEWER.read_text(encoding="utf-8")

        self.assertIn("requestedMode === 'no-connect'", source)
        self.assertIn("EasyMesh room sandbox", source)
        self.assertIn("NO CONNECT", source)
        self.assertNotIn('id="cameraMode"', source)
        self.assertNotIn('id="interactMode"', source)
        self.assertIn("commitDraggedRole(gesture, point)", source)

    def test_automatic_closed_loop_state_is_visible(self):
        source = VIEWER.read_text(encoding="utf-8")

        self.assertIn("automatic_actuation_ready", source)
        self.assertIn("AUTO BTM", source)
        self.assertIn("Action budget", source)
        self.assertIn("optimizerSubjectRole()", source)
        self.assertIn("Fleet check", source)
        self.assertIn("Stronger AP available", source)
        self.assertIn("reconciling fleet", source)

    def test_extender_outage_is_a_bounded_fronthaul_control(self):
        source = VIEWER.read_text(encoding="utf-8")

        self.assertIn("snapshot.presence_roles", source)
        self.assertIn("Disable fronthaul", source)
        self.assertIn("Restore fronthaul", source)
        self.assertIn("role !== 'gateway'", source)

    def test_candidate_outage_clears_old_optimizer_cards(self):
        source = VIEWER.read_text(encoding="utf-8")

        self.assertIn("optimizerState.status === 'unavailable'", source)
        self.assertIn("Automatic steering paused", source)
        self.assertIn("optimizer.measurement.unavailable') optimizerState = event.payload", source)


if __name__ == "__main__":
    unittest.main()
