"""Process lifecycle: admission and cooperative cancellation for one serving incarnation.

    starting --recovered--> serving --stop--> draining --released--> stopped
    serving | draining --lease lost--> lost

Serving admits new work. Draining admits none, lets in flight work finish under a still
renewed lease, and cancels the rest at the drain deadline. Cancellation is cooperative:
work checks the token only at safe boundaries, never between an effect's intent and its
outcome, so a cancel never tears a durable write.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from enum import Enum

from gameshark.errors import Cancelled, Draining


class Phase(Enum):
    STARTING = "starting"
    SERVING = "serving"
    DRAINING = "draining"
    STOPPED = "stopped"
    LOST = "lost"


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._next_id = 0
        self.reason = ""

    def cancel(self, reason: str) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self.reason = reason
            self._event.set()
            callbacks = list(self._callbacks.values())
            self._callbacks.clear()
        for cb in callbacks:
            cb()

    def on_cancel(self, cb: Callable[[], None]) -> Callable[[], None]:
        """Run cb once on cancellation (now, if already cancelled). Return an unsubscribe.

        Lets a blocked waiter wake on cancel without polling the token.
        """
        with self._lock:
            if not self._event.is_set():
                key = self._next_id
                self._next_id += 1
                self._callbacks[key] = cb
                return lambda: self._unsubscribe(key)
        cb()
        return lambda: None

    def _unsubscribe(self, key: int) -> None:
        with self._lock:
            self._callbacks.pop(key, None)

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        """Block up to timeout seconds. Return True once cancellation was requested."""
        return self._event.wait(timeout)

    def check(self) -> None:
        """Raise Cancelled at a safe boundary once cancellation was requested."""
        if self._event.is_set():
            raise Cancelled(self.reason)


class Lifecycle:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.phase = Phase.STARTING
        self.deadline: float | None = None
        self.token = CancelToken()

    def serve(self) -> None:
        with self._lock:
            if self.phase is Phase.STARTING:
                self.phase = Phase.SERVING

    def admit(self) -> None:
        """Gate new work. Raise Draining unless serving."""
        if self.phase is not Phase.SERVING:
            raise Draining(f"not admitting new work while {self.phase.value}")

    def begin_drain(self, deadline: float) -> bool:
        """Freeze admission. Return False if already draining, stopped, or lost."""
        with self._lock:
            if self.phase not in (Phase.STARTING, Phase.SERVING):
                return False
            self.phase = Phase.DRAINING
            self.deadline = deadline
            return True

    def lost(self, reason: str) -> None:
        with self._lock:
            self.phase = Phase.LOST
        self.token.cancel(reason)

    def stopped(self) -> None:
        with self._lock:
            if self.phase is not Phase.LOST:
                self.phase = Phase.STOPPED
