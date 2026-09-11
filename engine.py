from __future__ import annotations

from dataclasses import dataclass, field
import copy
import hashlib
import json
import queue
import re
import threading
from typing import Any, Callable

from .interactions import InteractionError, InteractiveMediumSession


@dataclass
class _Command:
    function: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    completed: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None


class RoomEngine:
    """Actor-style serialized owner of one interactive room session.

    HTTP handlers, controller workers and server-owned movement clocks call
    this facade. Only its worker thread enters mutable session methods. Reads
    that expire leases also pass through the actor. Display projections use a
    nonblocking read and never queue behind a medium transaction.
    """

    def __init__(self, session: InteractiveMediumSession) -> None:
        self._session = session
        self._commands: queue.Queue[_Command | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._lifecycle_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._started = False
        self._start_attempted = False
        self._accepting = True
        self._closed = False
        self._final_snapshot: dict[str, Any] | None = None
        self._final_documents: tuple[dict[str, Any] | None, dict[str, Any] | None] = (
            None,
            None,
        )
        self._command_results: dict[str, tuple[str, Any]] = {}
        session.set_command_executor(self._execute_callable)

    def _worker(self) -> None:
        self._thread_id = threading.get_ident()
        while True:
            command = self._commands.get()
            if command is None:
                return
            try:
                command.result = command.function(*command.args, **command.kwargs)
            except BaseException as error:
                command.error = error
            finally:
                command.completed.set()

    def _start_worker_locked(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._worker,
            name="room-demo-engine",
            daemon=True,
        )
        self._thread.start()

    def _execute_callable(
        self, function: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        if threading.get_ident() == self._thread_id:
            return function(*args, **kwargs)
        command = _Command(function, args, kwargs)
        # Admission and queue insertion are one transaction. Once close()
        # withdraws admission, no movement or HTTP command can land behind the
        # terminal command and wait on an actor that has already exited.
        with self._lifecycle_lock:
            if not self._accepting or self._closed:
                raise RuntimeError("room engine is closing")
            self._start_worker_locked()
            self._commands.put(command)
        command.completed.wait()
        if command.error is not None:
            raise command.error
        return command.result

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        return self._execute_callable(getattr(self._session, method), *args, **kwargs)

    @staticmethod
    def _validate_command_id(command_id: str) -> str:
        value = str(command_id or "")
        if not 8 <= len(value) <= 128 or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]*", value
        ):
            raise InteractionError(
                400,
                "invalid_command_id",
                "command_id must be 8..128 URL-safe letters, digits or ._:-"
            )
        return value

    def _mutation(
        self,
        operation: str,
        command_id: str,
        method: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        command_id = self._validate_command_id(command_id)
        material = json.dumps(
            {"operation": operation, "args": args, "kwargs": kwargs},
            sort_keys=True,
            separators=(",", ":"),
            # The session performs the authoritative finite-number check and
            # returns a precise invalid_position error. JSON still needs a
            # stable fingerprint for that rejected input.
            allow_nan=True,
        ).encode()
        fingerprint = hashlib.sha256(material).hexdigest()

        def execute() -> Any:
            cached = self._command_results.get(command_id)
            if cached is not None:
                if cached[0] != fingerprint:
                    raise InteractionError(
                        409,
                        "command_id_reused",
                        "command_id was already used for a different operation",
                    )
                return copy.deepcopy(cached[1])
            result = getattr(self._session, method)(*args, **kwargs)
            self._command_results[command_id] = (fingerprint, copy.deepcopy(result))
            while len(self._command_results) > 512:
                self._command_results.pop(next(iter(self._command_results)))
            self._session.store.emit(
                "interaction.command.completed",
                self._session.world_time(),
                {
                    "command_id": command_id,
                    "operation": operation,
                    "revision": result.get("revision")
                    if isinstance(result, dict)
                    else None,
                },
                producer="room-engine",
            )
            return copy.deepcopy(result)

        return self._execute_callable(execute)

    def start(self) -> None:
        self._start_attempted = True
        self._call("start")
        self._started = True

    def snapshot(self) -> dict[str, Any]:
        if self._closed:
            assert self._final_snapshot is not None
            return copy.deepcopy(self._final_snapshot)
        return self._call("snapshot")

    def projection_snapshot(self) -> dict[str, Any] | None:
        if self._closed:
            return copy.deepcopy(self._final_snapshot)
        return self._session.projection_snapshot()

    def acquire(self, owner: str, *, command_id: str) -> dict[str, Any]:
        return self._mutation("lease.acquire", command_id, "acquire", owner)

    def world_catalog(self) -> dict[str, Any]:
        return self._call("world_catalog")

    def select_traffic_probe(self, role: str, *, command_id: str, **kwargs: Any) -> dict[str, Any]:
        return self._mutation("traffic.probe.select", command_id, "select_traffic_probe", role, **kwargs)

    def apply_world(self, selection: Any, *, command_id: str, **kwargs: Any) -> dict[str, Any]:
        return self._mutation("world.apply", command_id, "apply_world", selection, **kwargs)

    def playback_control(self, action: str, *, command_id: str, **kwargs: Any) -> dict[str, Any]:
        return self._mutation("playback." + action, command_id, "playback_control", action, **kwargs)

    def renew(self, token: str, *, command_id: str) -> dict[str, Any]:
        return self._mutation("lease.renew", command_id, "renew", token)

    def release(self, token: str, *, command_id: str) -> dict[str, Any]:
        return self._mutation("lease.release", command_id, "release", token)

    def position(
        self, role: str, *, command_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        return self._mutation(
            f"role.{role}.position", command_id, "position", role, **kwargs
        )

    def presence(
        self, role: str, *, command_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        return self._mutation(
            f"role.{role}.presence", command_id, "presence", role, **kwargs
        )

    def move(self, role: str, *, command_id: str, **kwargs: Any) -> dict[str, Any]:
        return self._mutation(
            f"role.{role}.move", command_id, "move", role, **kwargs
        )

    def movement_control(
        self,
        movement_id: str,
        action: str,
        *,
        command_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self._mutation(
            f"movement.{movement_id}.{action}",
            command_id,
            "movement_control",
            movement_id,
            action,
            **kwargs,
        )

    def start_recording(self, *, command_id: str, **kwargs: Any) -> dict[str, Any]:
        return self._mutation(
            "recording.start", command_id, "start_recording", **kwargs
        )

    def stop_recording(self, *, command_id: str, **kwargs: Any) -> dict[str, Any]:
        return self._mutation(
            "recording.stop", command_id, "stop_recording", **kwargs
        )

    def recorded_world(self) -> dict[str, Any]:
        return self._call("recorded_world")

    def backhaul_action(self, action: Callable[[], Any], *, expected_epoch: int) -> Any:
        return self._call("backhaul_action", action, expected_epoch=expected_epoch)

    def steering_action(
        self,
        station_role: str,
        source_ap_role: str,
        target_ap_role: str,
        band: str,
        action: Callable[[], Any],
        *,
        expected_epoch: int | None = None,
    ) -> Any:
        """Serialize optimizer RF assistance with all interactive mutations."""
        return self._call(
            "steering_action",
            station_role,
            source_ap_role,
            target_ap_role,
            band,
            action,
            expected_epoch=expected_epoch,
        )

    def recorded_documents(
        self,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if self._closed:
            return copy.deepcopy(self._final_documents)
        return self._call("recorded_documents")

    def close(self) -> bool:
        with self._close_lock:
            return self._close()

    def _close(self) -> bool:
        with self._lifecycle_lock:
            if self._closed:
                return bool(
                    self._final_snapshot and self._final_snapshot.get("restored")
                )
            self._accepting = False
            self._start_worker_locked()
            thread = self._thread

            def finalize() -> tuple[
                bool,
                tuple[dict[str, Any] | None, dict[str, Any] | None],
                dict[str, Any],
            ]:
                try:
                    restored = bool(self._session.close())
                except BaseException as error:
                    try:
                        failed_snapshot = self._session.snapshot(expire_lease=False)
                    except BaseException:
                        failed_snapshot = {"schema": "easymesh.room-demo.interactions.v1"}
                    failed_snapshot.update(enabled=False, restored=False, fault=str(error))
                    self._final_snapshot = copy.deepcopy(failed_snapshot)
                    raise
                if not self._start_attempted:
                    # No medium connection or write was attempted, so there
                    # is no RF state to restore.
                    restored = True
                snapshot = self._session.snapshot()
                snapshot["restored"] = restored
                return restored, self._session.recorded_documents(), snapshot

            command = _Command(finalize, (), {})
            self._commands.put(command)

        command.completed.wait()
        if command.error is not None:
            with self._lifecycle_lock:
                if self._final_snapshot is None:
                    self._final_snapshot = {
                        "schema": "easymesh.room-demo.interactions.v1",
                        "enabled": False, "restored": False, "fault": str(command.error),
                    }
                self._closed = True
            self._commands.put(None)
            if thread is not None:
                thread.join(timeout=5)
            raise command.error

        restored, documents, snapshot = command.result
        self._final_documents = copy.deepcopy(documents)
        self._final_snapshot = copy.deepcopy(snapshot)
        with self._lifecycle_lock:
            self._closed = True
        self._commands.put(None)
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError("room engine worker did not stop")
        return restored
