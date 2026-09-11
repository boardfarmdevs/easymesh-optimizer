from __future__ import annotations

import copy
from bisect import bisect_right
import datetime as dt
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from .journal import BoundedJournal


class EventHistoryGap(ValueError):
    pass


class EventStore:
    """Thread-safe run state, ordered event journal, and SSE replay source."""

    def __init__(
        self,
        run_id: str,
        world: dict[str, Any],
        event_path: Path,
        *,
        persist: bool = True,
        asynchronous: bool = False,
        history_events: int | None = 4096,
        history_bytes: int | None = 16 * 1024 * 1024,
        journal_options: dict | None = None,
    ):
        self.run_id = run_id
        self.world = world
        self.initial_world = copy.deepcopy(world)
        self.event_path = event_path
        self.persist = persist
        self._events: list[dict[str, Any]] = []
        self._sequences: list[int] = []
        self._event_sizes: list[int] = []
        self._retained_bytes = 0
        self._history_discarded = 0
        self._history_events = history_events
        self._history_bytes = history_bytes
        self._last_sequence = 0
        self._last_event_hash = None
        if any(value is not None and value < 1 for value in (history_events, history_bytes)):
            raise ValueError("history bounds must be positive")
        self._journal = BoundedJournal(event_path, **(journal_options or {})) if persist and asynchronous else None
        self._condition = threading.Condition()
        self._started_monotonic = time.monotonic()
        first = world.get("generations", [{}])[0]
        base_positions = first.get("positions", {})
        base_presence = first.get("present", {})
        self._state: dict[str, Any] = {
            "schema": "easymesh.room-demo.state.v2",
            "run_id": run_id,
            "state": "preparing",
            "run_state": "preparing",
            "scenario_clock_state": "stopped",
            "interaction_state": "unowned",
            "optimizer_authority": "observe",
            "act_arm_state": "disarmed",
            "scenario": world["name"],
            "world_url": "/api/demo/world",
            "duration_ms": world["duration_ms"],
            "tick_ms": world["tick_ms"],
            "world_time_ms": 0,
            "clocks": {"run_elapsed_ms": 0, "scenario_time_ms": 0},
            "sequence": 0,
            "world_revision": 0,
            "world_epoch": 0,
            "environment_epoch": 0,
            "medium": {},
            "roles": {
                role: {
                    "kind": kind,
                    "base_position": copy.deepcopy(base_positions.get(role)),
                    "authoritative_position": copy.deepcopy(base_positions.get(role)),
                    "present": bool(base_presence.get(role, True)),
                    "control_state": "scripted",
                }
                for role, kind in sorted(world.get("roles", {}).items())
            },
            "leases": {},
            "movements": {},
            "recording": {},
            "optimizer": {},
            "network": {},
            "traffic_probe": {},
            "health": {},
            "outcome": None,
            "restored": False,
            "error": None,
            "latest": {},
            "evidence_digest": None,
            "state_digest": None,
        }

    @staticmethod
    def _event_hash(event: dict[str, Any]) -> str:
        unsigned = dict(event)
        unsigned.pop("event_hash", None)
        material = json.dumps(
            unsigned, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        return hashlib.sha256(material).hexdigest()

    def _set_run_state(self, value: str) -> None:
        self._state["state"] = value
        self._state["run_state"] = value

    def _state_hash(self) -> str:
        unsigned = copy.deepcopy(self._state)
        unsigned.pop("state_digest", None)
        material = json.dumps(
            unsigned, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        return hashlib.sha256(material).hexdigest()

    def _reduce(self, event: dict[str, Any]) -> None:
        kind = event["kind"]
        payload = event.get("payload") or {}
        self._state["clocks"] = {
            "run_elapsed_ms": int(event.get("run_elapsed_ms", event["world_time_ms"])),
            "scenario_time_ms": int(event.get("scenario_time_ms", event["world_time_ms"])),
        }
        self._state["world_time_ms"] = self._state["clocks"]["scenario_time_ms"]
        if "revision" in payload:
            self._state["world_revision"] = max(
                self._state["world_revision"], int(payload["revision"])
            )
        if "environment_epoch" in payload:
            self._state["environment_epoch"] = max(
                self._state["environment_epoch"], int(payload["environment_epoch"])
            )
        if kind == "room.world.committed":
            self._state["world_epoch"] += 1
            self.world = copy.deepcopy(payload["world"])
            self._state.update({"scenario": self.world["name"],
                                "duration_ms": self.world["duration_ms"],
                                "tick_ms": self.world["tick_ms"],
                                "movements": {}, "recording": {}, "optimizer": {}, "network": {}, "health": {}})
            self._state["roles"] = {
                role: {"kind": self.world["roles"].get(role, "station"),
                       "base_position": copy.deepcopy(value["position"]),
                       "authoritative_position": copy.deepcopy(value["position"]),
                       "present": value["present"], "control_state": "manual" if value["present"] else "absent"}
                for role, value in payload["roles"].items()
            }
            for stale_kind in list(self._state["latest"]):
                if stale_kind != kind and stale_kind.startswith(("room.", "interaction.movement.", "interaction.recording.", "network.", "optimizer.", "health.", "traffic.")):
                    del self._state["latest"][stale_kind]
        elif kind.startswith("interaction.playback."):
            self._state["playback"] = copy.deepcopy(payload["playback"])
            for role, value in payload.get("roles", {}).items():
                if role in self._state["roles"]:
                    self._state["roles"][role].update({
                        "authoritative_position": copy.deepcopy(value["position"]),
                        "present": bool(value["present"]),
                        "control_state": "scripted" if value["present"] else "absent",
                    })
        elif kind in {"room.position.committed", "room.presence.committed"}:
            role = payload.get("role")
            if role in self._state["roles"]:
                reduced = self._state["roles"][role]
                if payload.get("position") is not None:
                    reduced["authoritative_position"] = copy.deepcopy(payload["position"])
                if "present" in payload:
                    reduced["present"] = bool(payload["present"])
                reduced["control_state"] = (
                    "manual" if reduced["present"] else "absent"
                )
        elif kind.startswith("interaction.movement.") and payload.get("movement"):
            movement = copy.deepcopy(payload["movement"])
            movement_id = movement.get("id")
            role = movement.get("role")
            if movement_id:
                self._state["movements"][movement_id] = movement
                completed = [identity for identity, value in self._state["movements"].items()
                             if value.get("status") not in {"running", "paused"}]
                for identity in completed[:-128]:
                    del self._state["movements"][identity]
            if role in self._state["roles"]:
                if movement.get("position") is not None:
                    self._state["roles"][role]["authoritative_position"] = copy.deepcopy(
                        movement["position"]
                    )
                self._state["roles"][role]["control_state"] = (
                    "moving" if movement.get("status") in {"running", "paused"} else "manual"
                )
        elif kind == "interaction.lease.acquired":
            self._state["interaction_state"] = "leased"
            self._state["leases"] = {"active": copy.deepcopy(payload)}
        elif kind in {"interaction.lease.released", "interaction.lease.expired"}:
            self._state["interaction_state"] = "unowned"
            self._state["leases"] = {}
        elif kind.startswith("interaction.recording."):
            self._state["recording"] = copy.deepcopy(payload.get("recording") or payload)
        elif kind in {"rf.generation.applied", "rf.generation.noop"}:
            self._state["medium"].update({
                "instance_id": payload.get("daemon_instance_id")
                or self._state["medium"].get("instance_id"),
                "generation": payload.get("daemon_generation"),
                "last_change": kind,
                "changed_link_count": payload.get("changed_link_count", 0),
            })
        elif kind == "interaction.session.ready":
            self._state["medium"].update({
                "instance_id": payload.get("daemon_instance_id"),
                "generation": payload.get("daemon_generation"),
                "contaminated": False,
            })
        elif kind == "medium.external_write_detected":
            self._state["medium"].update({
                "contaminated": True,
                "contamination": copy.deepcopy(payload),
            })
        elif kind in {"optimizer.evaluation", "optimizer.measurement.unavailable"}:
            self._state["optimizer"] = copy.deepcopy(payload)
        elif kind == "optimizer.verification" and payload.get("policy_state"):
            optimizer = self._state["optimizer"]
            optimizer["last_verification"] = copy.deepcopy(payload)
            if optimizer.get("subject_role") == payload.get("subject_role"):
                optimizer["policy_state"] = copy.deepcopy(payload["policy_state"])
                optimizer["decision"] = {**optimizer.get("decision", {}), "action": "none",
                                         "reason": "target_association_observed" if payload["success"] else "steer_failure_backoff"}
        elif kind in {"optimizer.progress", "optimizer.measurement.waiting", "optimizer.environment.changed", "observation.inconsistent_rf_epoch"}:
            self._state["optimizer"]["progress"] = {
                **copy.deepcopy(payload), "kind": kind, "recorded_at": event["recorded_at"],
            }
            if kind != "optimizer.progress":
                self._state["optimizer"]["automatic_actuation_ready"] = False
                self._state["optimizer"]["fleet"] = {}
                self._state["optimizer"]["client_decisions"] = []
        elif kind == "traffic.probe.selected":
            self._state["traffic_probe"] = copy.deepcopy(payload["traffic_probe"])
            self._state["latest"].pop("traffic.sample", None)
        elif kind == "traffic.sample":
            probe = payload.get("traffic_probe") or {}
            selected = self._state["traffic_probe"]
            if selected and probe and (probe.get("role"), probe.get("selection")) != (selected.get("role"), selected.get("selection")):
                self._state["latest"].pop("traffic.sample", None)
        elif kind == "network.snapshot":
            self._state["network"] = copy.deepcopy(payload)
            probe = payload.get("traffic_probe") or {}
            if probe and probe.get("selection", 0) >= self._state["traffic_probe"].get("selection", 0):
                self._state["traffic_probe"] = copy.deepcopy(probe)
        elif kind == "health.sample":
            self._state["health"] = copy.deepcopy(payload)
        if kind in {"traffic.probe.selected", "network.snapshot"} and self._state["traffic_probe"]:
            network = self._state["network"]
            network["traffic_probe"] = copy.deepcopy(self._state["traffic_probe"])
            network["hero"] = next((client for client in network.get("clients", [])
                                    if client.get("role") == self._state["traffic_probe"].get("role")), None)

    def _publish(self, event: dict[str, Any]) -> dict[str, Any]:
        if event.get("schema") != "easymesh.room-demo.event.v1":
            raise ValueError("unsupported room-demo event schema")
        if event.get("run_id") != self.run_id:
            raise ValueError("event run_id does not match the active run")
        with self._condition:
            if event["sequence"] <= self._last_sequence:
                raise ValueError("event sequence is not strictly increasing")
            if event.get("event_hash"):
                if event["event_hash"] != self._event_hash(event):
                    raise ValueError("event hash does not match event contents")
                expected_previous = self._last_event_hash
                if event.get("previous_event_hash") != expected_previous:
                    raise ValueError("event hash chain is discontinuous")
            self._events.append(event)
            self._sequences.append(event["sequence"])
            encoded = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode()
            self._event_sizes.append(len(encoded))
            self._retained_bytes += len(encoded)
            self._last_sequence = event["sequence"]
            self._last_event_hash = event.get("event_hash")
            while ((self._history_events is not None and len(self._events) > self._history_events)
                   or (self._history_bytes is not None and self._retained_bytes > self._history_bytes)):
                self._retained_bytes -= self._event_sizes.pop(0)
                self._events.pop(0)
                self._sequences.pop(0)
                self._history_discarded += 1
            kind = event["kind"]
            self._state["sequence"] = event["sequence"]
            self._state["latest"][kind] = event
            self._reduce(event)
            if kind == "runner.preflight":
                self._set_run_state("ready")
            elif kind in {"scenario.started", "scenario.clock", "scenario.mark",
                          "scenario.generation"}:
                self._set_run_state("running")
                self._state["scenario_clock_state"] = "playing"
            elif kind == "rf.restore.started":
                self._set_run_state("restoring")
            elif kind == "rf.restore.completed":
                self._state["restored"] = bool(event["payload"].get("verified"))
            elif kind == "scenario.completed":
                payload = event["payload"]
                self._set_run_state(payload["outcome"])
                self._state["scenario_clock_state"] = "stopped"
                self._state["outcome"] = payload["outcome"]
                self._state["restored"] = bool(payload["restored"])
                self._state["error"] = payload.get("error")
                self._state["run_directory"] = payload.get("run_directory")
            elif kind == "demo.state":
                self._set_run_state(
                    event["payload"].get("state", self._state["state"])
                )
                mode = str(event["payload"].get("mode") or "")
                authority = mode.rsplit("-", 1)[-1]
                if authority == "act":
                    self._state["optimizer_authority"] = "act-capable"
                elif authority == "recommend":
                    self._state["optimizer_authority"] = "recommend"
                elif mode:
                    self._state["optimizer_authority"] = "observe"
            elif kind == "run.completed":
                payload = event["payload"]
                self._set_run_state(payload["outcome"])
                self._state["outcome"] = payload["outcome"]
                self._state["restored"] = bool(payload["restored"])
                self._state["error"] = payload.get("error")
                self._state["scenario_clock_state"] = "stopped"
            self._state["evidence_digest"] = event.get("event_hash")
            self._state["state_digest"] = self._state_hash()
            if self._journal is not None:
                self._journal.append(event, encoded)
            elif self.persist:
                self.event_path.parent.mkdir(parents=True, exist_ok=True)
                with self.event_path.open("a", encoding="utf-8") as stream:
                    stream.write(encoded.decode())
            self._condition.notify_all()
            return dict(event)

    def publish(self, event: dict[str, Any]) -> None:
        """Publish an already globally sequenced event.

        This compatibility entry point is useful when loading evidence. Live
        multi-producer code should call :meth:`emit` or :meth:`ingest`.
        """
        self._publish(dict(event))

    def emit(
        self,
        kind: str,
        world_time_ms: int,
        payload: dict[str, Any] | None = None,
        *,
        producer: str = "conductor",
    ) -> dict[str, Any]:
        """Create and publish one centrally sequenced live event."""
        with self._condition:
            sequence = self._last_sequence + 1
            event = {
                "schema": "easymesh.room-demo.event.v1",
                "run_id": self.run_id,
                "sequence": sequence,
                "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "world_time_ms": max(0, int(world_time_ms)),
                "run_elapsed_ms": max(
                    0, round((time.monotonic() - self._started_monotonic) * 1000)
                ),
                "scenario_time_ms": max(0, int(world_time_ms)),
                "kind": kind,
                "producer": producer,
                "payload": payload or {},
                "previous_event_hash": self._last_event_hash,
            }
            event["event_hash"] = self._event_hash(event)
            return self._publish(event)

    def ingest(self, event: dict[str, Any], *, producer: str = "wmdcfg") -> dict[str, Any]:
        """Re-sequence an event emitted by an independently sequenced producer."""
        if event.get("schema") != "easymesh.room-demo.event.v1":
            raise ValueError("unsupported room-demo event schema")
        if event.get("run_id") != self.run_id:
            raise ValueError("event run_id does not match the active run")
        payload = dict(event.get("payload") or {})
        payload.setdefault("producer_sequence", event.get("sequence"))
        return self.emit(
            str(event["kind"]),
            int(event.get("world_time_ms") or 0),
            payload,
            producer=producer,
        )

    def current(self) -> dict[str, Any]:
        with self._condition:
            return copy.deepcopy(self._state)

    def environment_epoch(self) -> int:
        with self._condition:
            return self._state["environment_epoch"]

    def clock_state(self) -> tuple[int, str]:
        with self._condition:
            return self._state["world_time_ms"], self._state["state"]

    def world_epoch(self) -> int:
        with self._condition:
            return self._state["world_epoch"]

    def role_present(self, role: str) -> bool:
        with self._condition:
            return bool(self._state["roles"].get(role, {}).get("present"))

    def wait_environment(self, epoch: int, timeout: float, stop: threading.Event) -> bool:
        with self._condition:
            self._condition.wait_for(
                lambda: self._state["environment_epoch"] != epoch or stop.is_set(), timeout
            )
            return stop.is_set()

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def current_world(self) -> dict[str, Any]:
        with self._condition:
            return copy.deepcopy(self.world)

    def all(self) -> list[dict[str, Any]]:
        with self._condition:
            return [dict(event) for event in self._events]

    def storage_status(self) -> dict:
        with self._condition:
            return {"history": {"retained_events": len(self._events), "retained_bytes": self._retained_bytes,
                    "maximum_events": self._history_events, "maximum_bytes": self._history_bytes,
                    "discarded_events": self._history_discarded,
                    "first_sequence": self._sequences[0] if self._sequences else self._last_sequence + 1,
                    "last_sequence": self._last_sequence, "truncated": self._history_discarded > 0},
                    "journal": self._journal.status() if self._journal is not None else
                    {"mode": "synchronous" if self.persist else "replay", "complete": True}}

    def close(self) -> None:
        if self._journal is not None:
            self._journal.close()

    def _after(self, sequence: int) -> list[dict[str, Any]]:
        first = self._sequences[0] if self._sequences else self._last_sequence + 1
        if self._history_discarded and sequence < first - 1:
            raise EventHistoryGap("requested event history expired; resynchronize from current state")
        return self._events[bisect_right(self._sequences, sequence):]

    def after(self, sequence: int) -> list[dict[str, Any]]:
        with self._condition:
            return self._after(sequence)

    def wait_after(self, sequence: int, timeout: float) -> list[dict[str, Any]]:
        with self._condition:
            events = self._after(sequence)
            if events:
                return events
            self._condition.wait(timeout)
            return self._after(sequence)

    @classmethod
    def from_evidence(cls, run_dir: Path) -> "EventStore":
        """Load a completed run without contacting the live lab."""
        world = json.loads((run_dir / "world.json").read_text(encoding="utf-8"))
        path = run_dir / "live-events.jsonl"
        index_path = run_dir / "journal-index.json"
        paths = [path]
        if index_path.exists():
            index = json.loads(index_path.read_text(encoding="utf-8"))
            if not index.get("complete") or index.get("retained_from_sequence", 1) != 1:
                raise ValueError("full replay unavailable: journal is incomplete or its beginning has expired")
            names = [entry["file"] for entry in index["segments"]]
            if any(Path(name).name != name for name in names):
                raise ValueError("invalid journal segment path")
            paths = [run_dir / name for name in names]
        rows = [
            json.loads(line)
            for segment in paths for line in segment.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not rows:
            raise ValueError(f"{path} contains no events")
        store = cls(str(rows[0]["run_id"]), world, path, persist=False, history_events=None, history_bytes=None)
        for event in rows:
            store.publish(event)
        return store
