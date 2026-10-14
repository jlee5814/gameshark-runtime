"""Durable event router.

An event is persisted before any handler runs (durable delivery), delivered to the
owning adapter, and acknowledged only after the handler commits. Unacknowledged
events replay on restart (invariant I5). Repeat delivery of the same event_id is
deduplicated by primary key, so at least once delivery never becomes duplicate
consequence (the effect journal absorbs the rest).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

from gameshark.clock import Clock
from gameshark.errors import AmbiguousEffect, BlockedEffect
from gameshark.lease import LeaseManager
from gameshark.lifecycle import CancelToken
from gameshark.store import write_txn


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
        try:
            self._dispatch(event)              # may raise CrashInjected or Ambiguous/Blocked
        except (AmbiguousEffect, BlockedEffect):
            self._set_status(event["event_id"], "dead")   # escalation, do not replay forever
            return "escalated"
        self._set_status(event["event_id"], "acked")
        return "acked"

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
