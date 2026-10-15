"""Durable event router.

An event is persisted before any handler runs (durable delivery), delivered to the
owning adapter, and acknowledged only after the handler commits. Unacknowledged
events replay on restart (invariant I5). Repeat delivery of the same event_id is
deduplicated by primary key, so at least once delivery never becomes duplicate
consequence (the effect journal absorbs the rest).

A handler whose outcome arrives later (an inference call on an executor) returns Deferred.
The event then stays pending and claimed in memory until finish acknowledges it or release
gives it back; replay and redelivery skip claimed events, so one event never runs twice at
once. Claims are memory only: after a crash the pending row replays as before.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from gameshark.clock import Clock
from gameshark.errors import AmbiguousEffect, BlockedEffect
from gameshark.lease import LeaseManager
from gameshark.lifecycle import CancelToken
from gameshark.store import write_txn


@dataclass(frozen=True)
class Deferred:
    """A handler's outcome arrives later; the event stays pending.

    retry_at None: the handler holds the event (claimed) and will finish or release it.
    retry_at set: nothing holds it; redeliver_due hands it back at or after that monotonic
    time. math.inf leaves it for replay by the next owner.
    """

    retry_at: float | None = None


class EventRouter:
    def __init__(
        self,
        conn: sqlite3.Connection,
        lease: LeaseManager,
        generation: int,
        clock: Clock,
        dispatch: Callable[[dict], None],
        cancel: CancelToken | None = None,
    ) -> None:
        self.conn = conn
        self.lease = lease
        self.generation = generation
        self.clock = clock
        self._dispatch = dispatch              # routes an event dict to its adapter.on_event
        self._cancel = cancel or CancelToken()
        self._claims: set[str] = set()          # deferred and held by a handler
        self._retry: dict[str, float] = {}     # deferred and released: event_id -> retry_at

    def _persist(self, event: dict) -> str:
        """Insert a new event or return the status of an existing one (dedup)."""
        now = self.clock.now()
        with write_txn(self.conn):
            self.lease.guard_in_txn(self.generation)
            existing = self.conn.execute(
                "SELECT status FROM events WHERE event_id=?", (event["event_id"],)
            ).fetchone()
            if existing is not None:
                return existing["status"]
            self.conn.execute(
                "INSERT INTO events(event_id, adapter, type, payload, status, "
                "delivered_count, created) VALUES(?,?,?,?, 'pending', 0, ?)",
                (event["event_id"], event["adapter"], event["type"],
                 json.dumps(event.get("payload", {})), now),
            )
            return "pending"

    def _set_status(self, event_id: str, status: str) -> None:
        with write_txn(self.conn):
            self.lease.guard_in_txn(self.generation)
            self.conn.execute(
                "UPDATE events SET status=?, delivered_count=delivered_count+1 WHERE event_id=?",
                (status, event_id),
            )

    def deliver(self, event: dict) -> str:
        """Durably accept and handle one event. Return a short outcome label."""
        status = self._persist(event)
        if status == "acked":
            return "duplicate"                 # already handled, dedup
        if status == "dead":
            return "escalated"
        return self._handle(event)

    def _handle(self, event: dict) -> str:
        # Safe boundary: a cancelled drain leaves the event pending for the successor.
        self._cancel.check()
        event_id = event["event_id"]
        if event_id in self._claims:
            return "claimed"                   # its deferred outcome is still running
        self._retry.pop(event_id, None)
        try:
            out = self._dispatch(event)        # may raise CrashInjected or Ambiguous/Blocked
        except (AmbiguousEffect, BlockedEffect):
            self._set_status(event_id, "dead")   # escalation, do not replay forever
            return "escalated"
        if isinstance(out, Deferred):
            if out.retry_at is None:
                self._claims.add(event_id)
            else:
                self._retry[event_id] = out.retry_at
            return "deferred"
        self._set_status(event_id, "acked")
        return "acked"

    def finish(self, event_id: str, fn: Callable[[], None]) -> str:
        """Complete a deferred event: run its continuation, then acknowledge it.

        Same outcome rules as a synchronous handler. Any other exception leaves the event
        pending and unclaimed, for redelivery or the next owner's replay.
        """
        self._claims.discard(event_id)
        try:
            fn()
        except (AmbiguousEffect, BlockedEffect):
            self._set_status(event_id, "dead")
            return "escalated"
        self._set_status(event_id, "acked")
        return "acked"

    def release(self, event_id: str, retry_at: float = math.inf) -> None:
        """Give a deferred event back without acknowledging it. It stays pending."""
        self._claims.discard(event_id)
        self._retry[event_id] = retry_at

    def release_all(self) -> None:
        """Forget every claim and retry time (ownership ended); rows stay pending."""
        self._claims.clear()
        self._retry.clear()

    def claimed(self) -> frozenset[str]:
        return frozenset(self._claims)

    def redeliver_due(self, now: float | None = None) -> list[str]:
        """Redeliver released events whose retry time has come. Same event, same handler,
        so an inference owner reissues its same call_id."""
        now = time.monotonic() if now is None else now
        due = sorted((t, e) for e, t in self._retry.items() if t <= now)
        outcomes = []
        for _, event_id in due:
            event = self._load(event_id)
            if event is None:
                self._retry.pop(event_id, None)
                continue
            outcomes.append(self._handle(event))
        return outcomes

    def _load(self, event_id: str) -> dict | None:
        r = self.conn.execute(
            "SELECT event_id, adapter, type, payload FROM events WHERE event_id=? AND "
            "status='pending'", (event_id,)).fetchone()
        if r is None:
            return None
        return {"event_id": r["event_id"], "adapter": r["adapter"], "type": r["type"],
                "payload": json.loads(r["payload"])}

    def replay(self) -> list[str]:
        """Redeliver every unacknowledged event on recovery."""
        rows = self.conn.execute(
            "SELECT event_id, adapter, type, payload FROM events WHERE status='pending' "
            "ORDER BY created"
        ).fetchall()
        outcomes = []
        for r in rows:
            event = {
                "event_id": r["event_id"],
                "adapter": r["adapter"],
                "type": r["type"],
                "payload": json.loads(r["payload"]),
            }
            outcomes.append(self._handle(event))
        return outcomes

    def status(self, event_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM events WHERE event_id=?", (event_id,)
        ).fetchone()
        return row["status"] if row else None
