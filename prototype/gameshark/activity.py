"""Activity projection: what the runtime is executing right now, for surfaces to render.

A read only, in memory view. It holds no connection and writes nothing durable, so an
avatar that follows it can never change identity, ownership, presence, duties, or
inference. A restart starts it empty; replayed events and the next poll rebuild it.

Two sources, both live:

- work spans, opened by an adapter around non inference execution of a responsibility
  (a GitHub poll, an event handler). Spans are a multiset keyed by token, so several
  concurrent spans for one or many responsibilities each count, and the projection stays
  working until the last one closes. A span closes in finally, on success, failure,
  cancellation, or ownership loss alike.
- inference calls, read from the executor's in flight tasks, the single record of which
  calls are running. The executor removes a task on commit, failure, abort, and drain, so
  no separate bookkeeping can drift from it.

Coordinator thread only, like every other reader of supervisor state.
"""

from __future__ import annotations

import itertools
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager

IDLE, LISTENING, THINKING, WORKING = "idle", "listening", "thinking", "working"
NOTIFICATION, BLOCKED, DISCONNECTED = "notification", "blocked", "disconnected"
# Every state a surface can show. The runtime projects only idle, thinking, working, and
# blocked; listening and notification are local UI facts, disconnected is the link's.
STATES = (IDLE, LISTENING, THINKING, WORKING, NOTIFICATION, BLOCKED, DISCONNECTED)

Calls = Callable[[], list[tuple[str, str, str]]]


def derive(work: bool, thinking: bool, blocked: bool) -> str:
    """The runtime's summary state. Working outranks thinking: a poll is short and a reply
    is long, so the reverse order would hide work behind every reply."""
    if work:
        return WORKING
    if thinking:
        return THINKING
    if blocked:
        return BLOCKED
    return IDLE


class ActivityTracker:
    def __init__(self) -> None:
        self._spans: dict[int, tuple[str, str]] = {}   # token -> (responsibility_id, kind)
        self._tokens = itertools.count(1)
        self.calls: Calls = list                         # bound to the executor at start
        self.on_begin: Callable[[], None] = lambda: None

    @contextmanager
    def work(self, responsibility_id: str, kind: str) -> Iterator[None]:
        """Mark a responsibility as executing for the duration of the block."""
        first = not self._spans
        token = next(self._tokens)
        self._spans[token] = (responsibility_id, kind)
        try:
            if first:
                self.on_begin()  # push now: the work may block the coordinator for a while
            yield
        finally:
            self._spans.pop(token, None)

    @property
    def working(self) -> bool:
        return bool(self._spans)

    def view(self, blocked: list[str]) -> dict:
        counts = Counter(self._spans.values())
        work = [{"responsibility_id": rid, "kind": kind, "count": n}
                for (rid, kind), n in sorted(counts.items())]
        inference = [{"call_id": c, "conversation_id": conv, "stage": stage}
                     for c, conv, stage in sorted(self.calls())]
        return {"state": derive(bool(work), bool(inference), bool(blocked)),
                "inference": inference, "work": work, "blocked": sorted(blocked)}
