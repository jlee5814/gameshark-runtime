"""Injectable clock so lease expiry stays deterministic in tests."""

from __future__ import annotations

import time


class Clock:
    """Wall clock. Lease expiry persists as an absolute timestamp."""

    def now(self) -> float:
        return time.time()


class FakeClock(Clock):
    """Manually advanced clock for deterministic expiry tests."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds
