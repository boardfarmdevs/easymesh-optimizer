from types import SimpleNamespace
from unittest import TestCase, mock

from room_demo.cli import _interactive
from wmdcfg.actuator import ActuatorError


class BackhaulOptionTests(TestCase):
    def test_geometry_rf_does_not_require_disabling_client_policy(self):
        for mode in ("act", "stimulus", "observe"):
            options = SimpleNamespace(model_backhaul=True, adaptive_backhaul=False,
                                      profiling=True, mode=mode, yes_act=True, max_actions=100)
            with mock.patch("room_demo.cli._paths", side_effect=RuntimeError("validated options")):
                with self.assertRaisesRegex(RuntimeError, "validated options"):
                    _interactive(options)

    def test_external_parent_selection_remains_excluded_from_profiling(self):
        options = SimpleNamespace(model_backhaul=False, adaptive_backhaul=True, profiling=True)
        with self.assertRaisesRegex(ActuatorError, "excludes --adaptive-backhaul"):
            _interactive(options)
        options.model_backhaul = True
        with self.assertRaisesRegex(ActuatorError, "select one parent authority"):
            _interactive(options)
