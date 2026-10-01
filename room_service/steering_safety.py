from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
import threading
import time
from typing import Callable

from .interactions import InteractionError


@dataclass
class ClientSafety:
    context: tuple = ()
    generation: int = 0
    last_request: float = -math.inf
    failures: deque = field(default_factory=deque)
    moves: deque = field(default_factory=lambda: deque(maxlen=4))
    paused: str | None = None
    last_failure: str | None = None
    pending: int | None = None


class SteeringSafety:
    """Bound automatic requests without imposing a lifetime demo budget."""

    def __init__(self, request_limit: int = 300, *, clock: Callable[[], float] = time.monotonic):
        if isinstance(request_limit, bool) or not isinstance(request_limit, int) or request_limit < 1:
            raise ValueError("steering request limit must be a positive integer")
        self.request_limit = request_limit
        self.window_seconds = 60
        self.client_interval_seconds = 5
        self.failure_limit = 3
        self.failure_window_seconds = 180
        self.oscillation_limit = 4
        self.oscillation_window_seconds = 60
        self.clock = clock
        self._lock = threading.RLock()
        self._clients: dict[str, ClientSafety] = {}
        self._requests: deque = deque()
        self._tickets: dict[int, tuple[str, int, tuple]] = {}
        self._total = 0
        self._revision = 0
        self._pause_revision = 0
        self._world = None

    def _prune(self, now):
        while self._requests and self._requests[0] <= now - self.window_seconds:
            self._requests.popleft()
        for client in self._clients.values():
            while client.failures and client.failures[0] <= now - self.failure_window_seconds:
                client.failures.popleft()
            while client.moves and client.moves[0][0] <= now - self.oscillation_window_seconds:
                client.moves.popleft()

    def sync(self, world, contexts: dict[str, tuple]):
        with self._lock:
            changed_world = self._world != world
            if changed_world:
                self._world = world
                self._tickets.clear()
                self._pause_revision += 1
            for station, context in contexts.items():
                client = self._clients.setdefault(station, ClientSafety())
                if changed_world or client.context != context:
                    client.context = context
                    client.generation += 1
                    client.failures.clear()
                    client.moves.clear()
                    client.last_failure = None
                    if client.paused:
                        client.paused = None
                        self._pause_revision += 1
                    if changed_world:
                        client.pending = None
                    self._revision += 1
            if changed_world:
                for station in set(self._clients) - set(contexts):
                    del self._clients[station]
                self._revision += 1

    def _blocked(self, station, source, target, now):
        client = self._clients.setdefault(station, ClientSafety())
        if client.paused:
            return client.paused
        if client.pending is not None:
            return "steering_request_pending"
        if now - client.last_request < self.client_interval_seconds:
            return "steering_client_cooldown"
        if len(self._requests) >= self.request_limit:
            return "steering_rate_limited"
        moves = [(item[1], item[2]) for item in client.moves] + [(source, target)]
        recent = moves[-self.oscillation_limit:]
        if len(recent) == self.oscillation_limit and source and target and source != target and all(
            previous == (following[1], following[0]) for previous, following in zip(recent, recent[1:])
        ):
            client.paused = "steering_oscillation_paused"
            self._pause_revision += 1
            self._revision += 1
            return client.paused
        return None

    def check(self, station, source, target):
        with self._lock:
            now = self.clock()
            self._prune(now)
            return self._blocked(station, source, target, now)

    def reserve(self, station, source, target):
        with self._lock:
            now = self.clock()
            self._prune(now)
            blocked = self._blocked(station, source, target, now)
            if blocked:
                return None, blocked
            client = self._clients[station]
            self._total += 1
            ticket = self._total
            self._tickets[ticket] = (station, client.generation, client.context)
            self._requests.append(now)
            client.last_request = now
            client.pending = ticket
            client.moves.append((now, source, target))
            self._revision += 1
            return ticket, None

    def complete(self, ticket, success, reason=""):
        with self._lock:
            record = self._tickets.pop(ticket, None)
            if record is None:
                return
            station, generation, context = record
            client = self._clients.get(station)
            if client is None:
                return
            if client.pending == ticket:
                client.pending = None
            self._revision += 1
            if client.generation != generation or client.context != context:
                return
            now = self.clock()
            self._prune(now)
            if success is None:
                return
            if success:
                client.failures.clear()
                client.last_failure = None
            else:
                client.failures.append(now)
                client.last_failure = str(reason)
                if len(client.failures) >= self.failure_limit:
                    client.paused = "steering_failures_paused"
                    self._pause_revision += 1

    def snapshot(self):
        with self._lock:
            now = self.clock()
            self._prune(now)
            limited = len(self._requests) >= self.request_limit
            paused = [
                {"sta_mac": station, "reason": client.paused, "failures": len(client.failures),
                 "last_failure": client.last_failure}
                for station, client in sorted(self._clients.items()) if client.paused
            ]
            return {
                "schema": "easymesh.steering-safety.v1", "enabled": True, "resume_supported": True,
                "revision": self._revision, "pause_revision": self._pause_revision,
                "state": "paused" if paused else "rate_limited" if limited else "ready",
                "paused_clients": paused, "requests_in_window": len(self._requests),
                "request_limit": self.request_limit, "window_seconds": self.window_seconds,
                "rate_retry_seconds": math.ceil(self._requests[0] + self.window_seconds - now) if limited else 0,
                "client_interval_seconds": self.client_interval_seconds,
                "failure_limit": self.failure_limit, "failure_window_seconds": self.failure_window_seconds,
                "oscillation_limit": self.oscillation_limit, "oscillation_window_seconds": self.oscillation_window_seconds,
                "total_requests": self._total,
            }

    def resume(self, expected_pause_revision):
        with self._lock:
            if isinstance(expected_pause_revision, bool) or not isinstance(expected_pause_revision, int):
                raise InteractionError(400, "invalid_safety_revision", "expected_pause_revision must be an integer")
            if expected_pause_revision != self._pause_revision:
                raise InteractionError(409, "steering_safety_changed", "Steering protection changed; review it before resuming")
            resumed = []
            for station, client in self._clients.items():
                if client.paused:
                    resumed.append(station)
                    client.paused = None
                    client.failures.clear()
                    client.moves.clear()
                    client.last_failure = None
                    client.generation += 1
            if resumed:
                self._pause_revision += 1
                self._revision += 1
            return {**self.snapshot(), "resumed_clients": sorted(resumed)}
