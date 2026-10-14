"""Durable capability grants.

Grants persist in SQLite, so authorization is a property of durable state, not of a
running object. A revoked capability stays revoked across restarts because every check
reads the table. Granting is additive on first registration; revocation is explicit
and never silently restored (invariant I6, strengthened to survive restart).
"""

from __future__ import annotations

import sqlite3

from gameshark.clock import Clock
from gameshark.store import write_txn


class GrantStore:
    def __init__(self, conn: sqlite3.Connection, agent_id: str, clock: Clock) -> None:
        self.conn = conn
        self.agent_id = agent_id
        self.clock = clock

    def ensure_granted(self, adapter: str, caps: frozenset[str]) -> None:
        """Record caps as granted on first registration without overriding a revocation.

        An INSERT OR IGNORE keeps any prior row, so a capability revoked earlier stays
        revoked even if the adapter re-registers after a restart.
        """
        now = self.clock.now()
        with write_txn(self.conn):
            for cap in caps:
                self.conn.execute(
                    "INSERT OR IGNORE INTO grants(agent_id, adapter, cap, granted, updated) "
                    "VALUES(?,?,?,1,?)",
                    (self.agent_id, adapter, cap, now),
                )

    def revoke(self, adapter: str, cap: str) -> None:
        now = self.clock.now()
        with write_txn(self.conn):
            self.conn.execute(
                "INSERT INTO grants(agent_id, adapter, cap, granted, updated) VALUES(?,?,?,0,?) "
                "ON CONFLICT(agent_id, adapter, cap) DO UPDATE SET granted=0, updated=excluded.updated",
                (self.agent_id, adapter, cap, now),
            )

    def effective(self, adapter: str, cap: str) -> bool:
        row = self.conn.execute(
            "SELECT granted FROM grants WHERE agent_id=? AND adapter=? AND cap=?",
            (self.agent_id, adapter, cap),
        ).fetchone()
        return bool(row and row["granted"] == 1)

    def effective_caps(self, adapter: str) -> frozenset[str]:
        rows = self.conn.execute(
            "SELECT cap FROM grants WHERE agent_id=? AND adapter=? AND granted=1",
            (self.agent_id, adapter),
        ).fetchall()
        return frozenset(r["cap"] for r in rows)
