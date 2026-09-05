from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import Mock, patch

if sys.version_info < (3, 9):
    raise unittest.SkipTest("optimizer runtime requires Python 3.9 or newer")

from optimizer.model import ClientObservation, MeshHealth, Snapshot
from optimizer.candidates import CandidateMetricsError, CandidateMetricsUnavailable
from optimizer.policy import Decision, Evaluation, PolicyConfig
from optimizer.state import ClientPolicyState, PolicyState
from room_demo.conductor import (
    LiveConductor,
    _deferred_state,
    _fleet_status,
    _interrupted_measurement_state,
    _ranked_action_batch,
    _simulated_bss_channels,
    _single_action_state,
)
from room_demo.events import EventStore


class ConductorProjectionTests(unittest.TestCase):
    def _run_optimizer(self, observations, *, interactive=True, evaluation_state=None):
        conductor, store = self._conductor()
        conductor.interactive = interactive
        conductor.mode = "act"
        conductor.manifest.update({"policy": "policy.yaml", "optimizer": {
            "allow_simulated_candidates": True, "request_only": True,
            "interval_seconds": 5, "action_window_ms": [0, 1000], "max_actions": 10,
        }})
        store.emit("demo.state", 0, {"state": "running"})
        snapshot = Snapshot(
            schema_version=1, sequence=1, controller_url="http://controller",
            observed_at="2026-09-03T00:00:00Z", health=MeshHealth(1, 1),
            clients=(), candidates=(),
        )
        observer = Mock()
        observer.observe.side_effect = [snapshot if item is None else item for item in observations]
        policy = Mock(config=PolicyConfig())
        policy.evaluate.return_value = Evaluation("hash", (Decision(
            sta_mac=conductor.hero_mac, action="none", reason="test_observed",
            source_bssid="02:00:00:00:01:01",
        ),), evaluation_state or PolicyState())
        provider = Mock(last_raw=[{"request": {}, "error": "HTTP 504"}], last_selected_sta_macs=set())
        actuator = Mock()
        with patch("room_demo.conductor.load_policy", return_value=PolicyConfig()), \
             patch("room_demo.conductor._simulated_bss_channels", return_value={}), \
             patch("room_demo.conductor.ThresholdPolicy", return_value=policy), \
             patch("room_demo.conductor.ControllerCandidateProvider", return_value=provider), \
             patch("room_demo.conductor.ControllerObserver", side_effect=[observer, Mock()]), \
             patch("room_demo.conductor.SteerActuator", return_value=actuator), \
             patch.object(conductor, "_sleep", side_effect=[False] * (len(observations) - 1) + [True]) as sleeper:
            conductor._run_worker("optimizer", conductor._optimizer_worker)
        self.assertFalse(conductor._candidate_active.is_set())
        self.assertFalse(conductor._controller_lock.locked())
        return conductor, store, policy, actuator, sleeper

    def test_interactive_measurement_outage_retries_without_steering(self):
        conductor, store, policy, actuator, sleeper = self._run_optimizer([
            CandidateMetricsUnavailable("HTTP 504"), CandidateMetricsUnavailable("HTTP 504"), None,
        ])
        self.assertEqual(conductor.errors, [])
        self.assertEqual(len(conductor.warnings), 2)
        self.assertEqual(store.current()["state"], "running")
        self.assertEqual(policy.evaluate.call_count, 1)
        self.assertEqual(conductor.action_attempts, 0)
        actuator.execute.assert_not_called()
        self.assertEqual([call.args[0] for call in sleeper.call_args_list[:2]], [5, 10])
        unavailable = store.current()["latest"]["optimizer.measurement.unavailable"]["payload"]
        self.assertEqual(unavailable["consecutive_failures"], 2)
        self.assertFalse(unavailable["automatic_actuation_ready"])
        self.assertFalse(unavailable["fleet"]["converged"])
        self.assertEqual(len(unavailable["failed_transactions"]), 1)
        self.assertEqual(store.current()["optimizer"]["decision"]["reason"], "test_observed")
        self.assertNotIn("status", store.current()["optimizer"])

    def test_outage_backoff_is_capped_and_resets_after_a_good_measurement(self):
        failures = [CandidateMetricsUnavailable("HTTP 504") for _index in range(5)]
        conductor, store, _policy, actuator, sleeper = self._run_optimizer(
            failures + [None, CandidateMetricsUnavailable("HTTP 504")]
        )
        waits = [call.args[0] for call in sleeper.call_args_list]
        self.assertEqual(waits[:5], [5, 10, 20, 30, 30])
        self.assertEqual(waits[-1], 5)
        self.assertEqual(conductor.errors, [])
        self.assertEqual(store.current()["optimizer"]["status"], "unavailable")
        actuator.execute.assert_not_called()

    def test_outage_restarts_an_unacted_hold_before_the_next_evaluation(self):
        holding = PolicyState((ClientPolicyState(
            sta_mac="02:00:00:00:0c:00", phase="holding",
            condition_since="2026-09-03T00:00:00Z", last_action_at="2026-09-02T00:00:00Z",
        ),))
        conductor, _store, policy, _actuator, _sleeper = self._run_optimizer(
            [None, CandidateMetricsUnavailable("HTTP 504"), None], evaluation_state=holding,
        )
        prior = policy.evaluate.call_args_list[-1].args[1].for_sta(conductor.hero_mac)
        self.assertEqual(prior.phase, "stable")
        self.assertIsNone(prior.condition_since)
        self.assertEqual(prior.last_action_at, "2026-09-02T00:00:00Z")

    def test_outage_keeps_pending_cooldown_and_backoff_history(self):
        for phase in ["pending", "cooldown", "backoff"]:
            prior = PolicyState((ClientPolicyState(
                sta_mac="02:00:00:00:0c:00", phase=phase,
                target_bssid="02:00:00:00:04:01", failure_count=2,
                pending_since="2026-09-03T00:00:00Z", cooldown_until="2026-09-03T00:00:30Z",
                backoff_until="2026-09-03T00:01:00Z", last_action_at="2026-09-03T00:00:00Z",
            ),))
            self.assertEqual(_interrupted_measurement_state(prior), prior)

    def test_scripted_measurement_outage_still_fails_closed(self):
        conductor, _store, policy, actuator, sleeper = self._run_optimizer(
            [CandidateMetricsUnavailable("HTTP 504")], interactive=False,
        )
        self.assertEqual(len(conductor.errors), 1)
        policy.evaluate.assert_not_called()
        actuator.execute.assert_not_called()
        sleeper.assert_not_called()

    def test_invalid_candidate_measurement_still_fails_closed_in_interactive_mode(self):
        conductor, _store, policy, actuator, sleeper = self._run_optimizer(
            [CandidateMetricsError("unexpected measurement")],
        )
        self.assertEqual(len(conductor.errors), 1)
        policy.evaluate.assert_not_called()
        actuator.execute.assert_not_called()
        sleeper.assert_not_called()

    def test_live_simulated_channels_are_mapped_per_bssid(self):
        plan = {"bindings": {
            "extender_1": {
                "role_type": "fronthaul_ap",
                "band_radios": {"6": {
                    "frequency_mhz": 5955,
                    "interfaces": [{
                        "mac": "02:00:00:00:01:60",
                        "frequency_mhz": 5955,
                    }],
                }},
            },
            "extender_2": {
                "role_type": "fronthaul_ap",
                "band_radios": {"6": {
                    "frequency_mhz": 6135,
                    "interfaces": [{
                        "mac": "02:00:00:00:02:60",
                        "frequency_mhz": 6135,
                    }],
                }},
            },
        }}

        self.assertEqual(_simulated_bss_channels(plan), {
            "02:00:00:00:01:60": 1,
            "02:00:00:00:02:60": 37,
        })

    def test_unexpected_worker_failure_is_retained_as_fatal(self):
        conductor, store = self._conductor()

        def broken():
            raise AttributeError("synthetic worker fault")

        conductor._run_worker("optimizer", broken)
        self.assertEqual(len(conductor.errors), 1)
        self.assertIn("synthetic worker fault", conductor.errors[0])
        event = store.current()["latest"]["worker.error"]
        self.assertTrue(event["payload"]["fatal"])

    def _conductor(self):
        world = {"name": "world", "duration_ms": 1000, "tick_ms": 100}
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = EventStore("run", world, Path(temp.name) / "live-events.jsonl")
        plan = {
            "bindings": {
                "sta_mobile_01": {
                    "role_type": "station",
                    "radio_permanent_mac": "02:00:00:00:0c:00",
                    "container": "wlan-client-007",
                },
                "sta_static_01": {
                    "role_type": "station",
                    "radio_permanent_mac": "02:00:00:00:03:00",
                    "container": "wlan-client",
                },
                "extender_1": {
                    "role_type": "fronthaul_ap",
                    "container": "bpiap",
                    "radio_permanent_mac": "02:00:00:00:01:00",
                    "band_radios": {
                        "5": {"interfaces": [{
                            "mac": "02:00:00:00:04:01",
                            "ssid": "private_ssid",
                        }]}
                    },
                },
                "gateway": {
                    "role_type": "fronthaul_ap",
                    "container": "bpibroadband",
                    "radio_permanent_mac": "02:00:00:00:00:00",
                    "band_radios": {
                        "5": {"interfaces": [{
                            "mac": "02:00:00:00:01:01",
                            "ssid": "private_ssid",
                        }]}
                    },
                }
            }
        }
        manifest = {
            "hero": {"role": "sta_mobile_01"},
            "traffic": {"target": "10.0.0.1", "timeout_seconds": 1},
        }
        return LiveConductor(
            store, plan, manifest, mode="recommend", repo_root=Path(temp.name)
        ), store

    def test_closed_action_window_preserves_holding_without_false_pending(self):
        sta = "02:00:00:00:0c:00"
        pending = ClientPolicyState(
            sta_mac=sta,
            phase="pending",
            source_bssid="02:00:00:00:01:01",
            target_bssid="02:00:00:00:04:01",
            condition_since="2026-09-03T00:00:00Z",
            pending_since="2026-09-03T00:00:10Z",
            last_action_at="2026-09-03T00:00:10Z",
        )
        evaluation = Evaluation(
            "hash",
            (Decision(
                sta_mac=sta,
                action="steer",
                reason="threshold_margin_hold_satisfied",
                source_bssid="02:00:00:00:01:01",
                target_bssid="02:00:00:00:04:01",
            ),),
            PolicyState((pending,)),
        )
        state = _deferred_state(PolicyState(), evaluation).for_sta(sta)
        self.assertEqual(state.phase, "holding")
        self.assertIsNone(state.pending_since)
        self.assertIsNone(state.last_action_at)

    def test_single_fleet_action_leaves_other_client_eligible(self):
        first = "02:00:00:00:03:00"
        second = "02:00:00:00:0c:00"
        pending = tuple(
            ClientPolicyState(
                sta_mac=sta,
                phase="pending",
                source_bssid="02:00:00:00:01:01",
                target_bssid="02:00:00:00:04:01",
                condition_since="2026-09-03T00:00:00Z",
                pending_since="2026-09-03T00:00:10Z",
                last_action_at="2026-09-03T00:00:10Z",
            )
            for sta in (first, second)
        )
        evaluation = Evaluation(
            "hash",
            tuple(Decision(
                sta_mac=sta,
                action="steer",
                reason="threshold_margin_hold_satisfied",
                source_bssid="02:00:00:00:01:01",
                target_bssid="02:00:00:00:04:01",
            ) for sta in (first, second)),
            PolicyState(pending),
        )

        state = _single_action_state(PolicyState(), evaluation, first)

        self.assertEqual(state.for_sta(first).phase, "pending")
        self.assertEqual(state.for_sta(second).phase, "holding")
        self.assertIsNone(state.for_sta(second).pending_since)

    def test_interactive_batch_prioritizes_weak_links_and_honors_limit(self):
        decisions = (
            Decision(
                sta_mac="02:00:00:00:03:00", action="steer", reason="ready",
                source_bssid="02:00:00:00:01:01",
                target_bssid="02:00:00:00:04:01",
                current_rcpi=110, target_rcpi=150,
            ),
            Decision(
                sta_mac="02:00:00:00:04:00", action="steer", reason="ready",
                source_bssid="02:00:00:00:01:01",
                target_bssid="02:00:00:00:04:01",
                current_rcpi=80, target_rcpi=100,
            ),
            Decision(
                sta_mac="02:00:00:00:05:00", action="steer", reason="ready",
                source_bssid="02:00:00:00:01:01",
                target_bssid="02:00:00:00:04:01",
                current_rcpi=80, target_rcpi=130,
            ),
            Decision(
                sta_mac="02:00:00:00:06:00", action="hold", reason="stable",
                source_bssid="02:00:00:00:01:01", current_rcpi=60,
            ),
        )

        batch = _ranked_action_batch(decisions, 2)

        self.assertEqual(
            [item.sta_mac for item in batch],
            ["02:00:00:00:05:00", "02:00:00:00:04:00"],
        )

    def test_fleet_convergence_uses_measured_best_ap_not_hold_phase(self):
        clients = (
            ClientObservation(
                sta_mac="02:00:00:00:03:00",
                connected_device_id="02:00:00:00:00:01",
                connected_device_name="Agent-1",
                connected_bssid="02:00:00:00:01:01",
                rcpi=80,
                association_uptime_seconds=90,
                metric_observed_at="2026-09-03T00:00:00Z",
                measurement_source="associated_sta_link_metrics",
                band="5", ssid="private_ssid", cohort="private",
            ),
        )
        from optimizer.model import CandidateObservation
        snapshot = Snapshot(
            schema_version=1, sequence=0,
            observed_at="2026-09-03T00:00:00Z",
            controller_url="http://controller",
            health=MeshHealth(devices=5, clients=1, bsses=50),
            clients=clients,
            candidates=(CandidateObservation(
                sta_mac=clients[0].sta_mac,
                bssid="02:00:00:00:04:01",
                device_id="02:00:00:00:00:04",
                device_name="Extender-1",
                rcpi=138,
                metric_observed_at="2026-09-03T00:00:00Z",
                measurement_source="candidate",
                band="5",
            ),),
        )

        status = _fleet_status(snapshot, {clients[0].sta_mac})

        self.assertFalse(status["converged"])
        self.assertEqual(status["clients_with_stronger_ap"], 1)
        self.assertEqual(status["stronger_candidates"][0]["gain_rcpi"], 58)

    def test_interactive_action_window_is_not_bound_to_scenario_time(self):
        conductor, _store = self._conductor()
        conductor.interactive = True

        self.assertEqual(
            conductor._action_window(999_999, [150_000, 220_000]),
            (True, "stable_interactive_environment"),
        )

    def test_interactive_subject_is_the_last_present_moved_client(self):
        conductor, _store = self._conductor()
        conductor.interactive = True
        room = {
            "last_rf_role": "sta_static_01",
            "roles": {"sta_static_01": {"present": True}},
        }

        self.assertEqual(
            conductor._optimization_subject(room),
            ("sta_static_01", "02:00:00:00:03:00", "wlan-client"),
        )
        room["roles"]["sta_static_01"]["present"] = False
        self.assertIsNone(conductor._optimization_subject(room))

    def test_controller_observation_is_projected_to_world_role(self):
        conductor, _store = self._conductor()
        snapshot = Snapshot(
            schema_version=1,
            sequence=0,
            observed_at="2026-09-03T00:00:00Z",
            controller_url="http://controller",
            health=MeshHealth(devices=5, clients=1, bsses=50),
            clients=(ClientObservation(
                sta_mac="02:00:00:00:0c:00",
                connected_device_id="02:00:00:00:00:01",
                connected_device_name="Extender-4",
                connected_bssid="02:00:00:00:04:01",
                rcpi=138,
                association_uptime_seconds=90,
                metric_observed_at="2026-09-03T00:00:00Z",
                measurement_source="associated_sta_link_metrics",
                band="5",
                ssid="private_ssid",
                cohort="private",
            ),),
            candidates=(),
        )
        payload = conductor._network_payload(snapshot)
        self.assertEqual(payload["hero"]["role"], "sta_mobile_01")
        self.assertEqual(payload["hero"]["connected_role"], "extender_1")
        self.assertEqual(payload["hero"]["connected_world_name"], "Extender-1")
        self.assertEqual(payload["hero"]["rssi_dbm"], -41)
        self.assertEqual(payload["cohorts"], {"private": 1, "iot": 0, "other": 0})

    def test_controller_backhaul_is_projected_by_bssid_not_display_ordinal(self):
        conductor, _store = self._conductor()
        topology = {
            "nodes": [
                {
                    "id": "02:00:00:00:10:20",
                    "name": "Agent-1",
                    "backhaulMedia": "Ethernet",
                    "haulTypes": [{"BSSList": [{
                        "BSSID": "02:00:00:00:01:01",
                    }]}],
                },
                {
                    "id": "02:00:00:00:44:20",
                    # Deliberately differs from the world role's ordinal.
                    "name": "Extender-4",
                    "backhaulMedia": "Wireless LAN",
                    "upstreamBSSID": "02:00:00:00:02:01",
                    "haulTypes": [{"BSSList": [
                        {
                            # Parent BSSID must not steal the child's role.
                            "BSSID": "02:00:00:00:01:01",
                            "vapMode": 1,
                        },
                        {
                            "BSSID": "02:00:00:00:04:01",
                            "vapMode": 0,
                        },
                    ]}],
                },
            ],
            "edges": [{
                "from": "02:00:00:00:10:20",
                "to": "02:00:00:00:44:20",
                "mediaType": "Wireless LAN",
                "band": 1,
                "channel": 36,
                "upstreamBSSID": "02:00:00:00:02:01",
                "backhaulSTA": "02:00:00:00:03:01",
                "signal": {"status": "fresh", "rcpi": 138, "rssi_dbm": -41},
            }],
        }

        mesh = conductor._topology_payload(topology)

        self.assertTrue(mesh["available"])
        self.assertEqual(mesh["unresolved_edges"], 0)
        self.assertEqual(
            {item["role"]: item["name"] for item in mesh["nodes"]},
            {"gateway": "Agent-1", "extender_1": "Extender-4"},
        )
        self.assertEqual(mesh["backhaul_edges"], [{
            "parent_role": "gateway",
            "child_role": "extender_1",
            "media_type": "Wireless LAN",
            "band": "5",
            "channel": 36,
            "upstream_bssid": "02:00:00:00:02:01",
            "backhaul_sta": "02:00:00:00:03:01",
            "signal": {"status": "fresh", "rcpi": 138, "rssi_dbm": -41},
        }])


if __name__ == "__main__":
    unittest.main()
