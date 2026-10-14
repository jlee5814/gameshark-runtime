"""Durable responsibility records.

A responsibility is a standing commitment an agent owns: a recurring party support
duty, a pull request to review. Records survive process death and drive recovery.
States: pending -> active -> done, or -> blocked (escalation).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from gameshark.clock import Clock
from gameshark.lease import LeaseManager
from gameshark.store import write_txn

VALID = {"pending", "active", "done", "blocked"}


@dataclass(frozen=True)
class Responsibility:
    id: str
    agent_id: str
    kind: str
    state: str
    payload: dict
    checkpoint: str | None


class ResponsibilityStore:
    def __init__(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        lease: LeaseManager,
        generation: int,
        clock: Clock,
    ) -> None:
        self.conn = conn
        self.agent_id = agent_id
        self.lease = lease
        self.generation = generation
        self.clock = clock

    def upsert(self, rid: str, kind: str, payload: dict, state: str = "pending") -> None:
        now = self.clock.now()
        with write_txn(self.conn):
            self.lease.guard_in_txn(self.generation)
            existing = self.conn.execute(
                "SELECT id FROM responsibilities WHERE id=?", (rid,)
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    "INSERT INTO responsibilities(id, agent_id, kind, state, payload, "
                    "checkpoint, created, updated) VALUES(?,?,?,?,?,?,?,?)",
                    (rid, self.agent_id, kind, state, json.dumps(payload), None, now, now),
                )

    def transition(self, rid: str, state: str, checkpoint: str | None = None) -> None:
        if state not in VALID:
            raise ValueError(f"invalid state {state}")
        now = self.clock.now()
        with write_txn(self.conn):
            self.lease.guard_in_txn(self.generation)
            self.conn.execute(
                "UPDATE responsibilities SET state=?, checkpoint=COALESCE(?, checkpoint), "
                "updated=? WHERE id=? AND agent_id=?",
                (state, checkpoint, now, rid, self.agent_id),
            )

    def get(self, rid: str) -> Responsibility | None:
        row = self.conn.execute(
            "SELECT * FROM responsibilities WHERE id=?", (rid,)
        ).fetchone()
        return self._row(row) if row else None

    def outstanding(self) -> list[Responsibility]:
        rows = self.conn.execute(
            "SELECT * FROM responsibilities WHERE agent_id=? AND state IN ('pending','active') "
            "ORDER BY created",
            (self.agent_id,),
        ).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row: sqlite3.Row) -> Responsibility:
        return Responsibility(
            id=row["id"],
            agent_id=row["agent_id"],
            kind=row["kind"],
            state=row["state"],
            payload=json.loads(row["payload"]),
            checkpoint=row["checkpoint"],
        )
