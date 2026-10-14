"""Durable effect journal: the exactly once consequence core.

Every consequential external action is wrapped. Intent is journaled before the effect
(invariant I3), outcome after (I4, I7). On restart, pending rows reconcile through a
remote probe.

Correctness rule (review issue 1): a missing probe result is never read as proof the
action did not run. A probe returns one of three outcomes:

- FOUND(id): the action is confirmed landed.
- ABSENT: the action is confirmed not landed. Trust this only when the destination is
  authoritative and consistent.
- UNKNOWN: the destination cannot confirm. The runtime refuses to guess; the effect
  blocks and the responsibility escalates.

Safe dispatch on ABSENT is sound only because the mock destination deduplicates by
operation id (destination side idempotency). The journal does not claim universal
exactly once external execution; see README proven-vs-unproven.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from gameshark.clock import Clock
from gameshark.errors import AmbiguousEffect, BlockedEffect
from gameshark.lease import LeaseManager


class ProbeKind(Enum):
    FOUND = "found"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProbeResult:
    kind: ProbeKind
    external_id: str | None = None

    @staticmethod
    def found(external_id: str) -> ProbeResult:
        return ProbeResult(ProbeKind.FOUND, external_id)

    @staticmethod
    def absent() -> ProbeResult:
        return ProbeResult(ProbeKind.ABSENT, None)

    @staticmethod
    def unknown() -> ProbeResult:
        return ProbeResult(ProbeKind.UNKNOWN, None)


@dataclass
class Effect:
    key: str                              # stable operation id, the dedup anchor everywhere
    intent: dict
    required_cap: str                     # capability the supervisor checks before dispatch
    adapter: str
    do: Callable[[int], str]              # performs the external action under a fence, returns id
    probe: Callable[[], ProbeResult]      # reports FOUND / ABSENT / UNKNOWN


class EffectJournal:
    def __init__(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        lease: LeaseManager,
        generation: int,
        clock: Clock,
        crash: Callable[[str], None] | None = None,
    ) -> None:
        self.conn = conn
        self.agent_id = agent_id
        self.lease = lease
        self.generation = generation
        self.clock = clock
        self._crash = crash or (lambda _label: None)

    def _get(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM effects WHERE key=?", (key,)).fetchone()

    def _write(self, key: str, status: str, intent: dict, external_id: str | None) -> None:
        """Guarded atomic upsert of one effect row (fencing enforced here)."""
        now = self.clock.now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.lease.guard_in_txn(self.generation)
            exists = self.conn.execute("SELECT key FROM effects WHERE key=?", (key,)).fetchone()
            if exists is None:
                self.conn.execute(
                    "INSERT INTO effects(key, agent_id, generation, status, external_id, "
                    "intent, created, updated) VALUES(?,?,?,?,?,?,?,?)",
                    (key, self.agent_id, self.generation, status, external_id,
                     json.dumps(intent), now, now),
                )
            else:
                self.conn.execute(
                    "UPDATE effects SET status=?, external_id=COALESCE(?, external_id), "
                    "generation=?, updated=? WHERE key=?",
                    (status, external_id, self.generation, now, key),
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def perform_once(self, eff: Effect) -> str:
        """Perform a consequential effect at most once in observable consequence."""
        self._crash("A_before_intent")

        prior = self._get(eff.key)
        if prior is not None:
            if prior["status"] == "done":
                return prior["external_id"]            # already done, suppress duplicate
            if prior["status"] == "blocked":
                raise BlockedEffect(f"effect {eff.key} is blocked and needs resolution")

        # Checkpoint intent before any external action (I3).
        self._write(eff.key, "pending", eff.intent, None)
        self._crash("B_after_intent")

        external_id = self._reconcile_or_dispatch(eff)

        # Checkpoint outcome (I4, I7).
        self._write(eff.key, "done", eff.intent, external_id)
        self._crash("D_after_done")
        return external_id

    def _reconcile_or_dispatch(self, eff: Effect) -> str:
        result = eff.probe()
        if result.kind is ProbeKind.FOUND:
            return result.external_id                  # already landed, adopt its id
        if result.kind is ProbeKind.UNKNOWN:
            self._write(eff.key, "blocked", eff.intent, None)
            raise AmbiguousEffect(f"effect {eff.key} unreconcilable; blocked for escalation")
        # ABSENT: dispatch under the current fence; destination idempotency makes a
        # duplicate dispatch a no op.
        external_id = eff.do(self.generation)
        self._crash("C_after_remote")
        return external_id

    def reconcile(self, eff: Effect) -> str | None:
        """Drive one pending effect to a terminal state on recovery.

        Returns the external id if completed, or None if it blocked.
        """
        row = self._get(eff.key)
        if row is None or row["status"] == "done":
            return row["external_id"] if row else None
        if row["status"] == "blocked":
            return None
        try:
            external_id = self._reconcile_or_dispatch(eff)
        except AmbiguousEffect:
            return None
        self._write(eff.key, "done", eff.intent, external_id)
        return external_id

    def pending(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM effects WHERE agent_id=? AND status='pending'", (self.agent_id,)
        ).fetchall()

    def status(self, key: str) -> str | None:
        row = self._get(key)
        return row["status"] if row else None
