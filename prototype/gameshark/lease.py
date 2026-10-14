"""Exclusive ownership with fencing.

A lease carries a monotonic generation. Acquiring a free or expired lease bumps the
generation. Every durable commit is guarded against the current generation, so a
stale holder whose lease was taken over cannot commit (invariant I2, fencing).
"""

from __future__ import annotations

import sqlite3

from gameshark.clock import Clock
from gameshark.errors import FencedError, LeaseDenied
from gameshark.store import write_txn


class LeaseManager:
    def __init__(self, conn: sqlite3.Connection, agent_id: str, clock: Clock) -> None:
        self.conn = conn
        self.agent_id = agent_id
        self.clock = clock

    def _row(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT owner_token, generation, expires_at FROM lease WHERE agent_id=?",
            (self.agent_id,),
        ).fetchone()

    def acquire(self, owner_token: str, ttl_s: float) -> int:
        """Acquire or renew. Return the owned generation. Raise LeaseDenied if held."""
        now = self.clock.now()
        with write_txn(self.conn):
            row = self._row()
            if row is None:
                gen = 1
                self.conn.execute(
                    "INSERT INTO lease(agent_id, owner_token, generation, expires_at) "
                    "VALUES(?,?,?,?)",
                    (self.agent_id, owner_token, gen, now + ttl_s),
                )
                return gen

            live = row["expires_at"] > now
            same_owner = row["owner_token"] == owner_token
            if live and not same_owner:
                raise LeaseDenied(
                    f"lease on {self.agent_id} held by another owner until {row['expires_at']}"
                )

            # Free, expired, or ours: take or renew.
            if same_owner and live:
                gen = row["generation"]  # renew keeps the generation
            else:
                gen = row["generation"] + 1  # takeover bumps the fence
            self.conn.execute(
                "UPDATE lease SET owner_token=?, generation=?, expires_at=? WHERE agent_id=?",
                (owner_token, gen, now + ttl_s, self.agent_id),
            )
            return gen

    def renew(self, owner_token: str, generation: int, ttl_s: float) -> None:
        """Extend our lease. Raise FencedError once another owner has taken it over.

        The caller must stop serving on FencedError: a takeover is permanent for this
        generation, so retrying can never succeed.
        """
        now = self.clock.now()
        with write_txn(self.conn):
            row = self._row()
            if row is None or row["owner_token"] != owner_token or row["generation"] != generation:
                raise FencedError("cannot renew a lease we no longer hold")
            self.conn.execute(
                "UPDATE lease SET expires_at=? WHERE agent_id=?", (now + ttl_s, self.agent_id)
            )

    def release(self, owner_token: str, generation: int) -> None:
        """Expire our lease now. A stale owner's release is a no op on the new lease."""
        with write_txn(self.conn):
            row = self._row()
            if row is not None and row["owner_token"] == owner_token and (
                row["generation"] == generation
            ):
                # Expire the lease immediately; keep the generation so the fence stays monotonic.
                self.conn.execute(
                    "UPDATE lease SET owner_token=NULL, expires_at=0 WHERE agent_id=?",
                    (self.agent_id,),
                )

    def current_generation(self) -> int:
        row = self._row()
        return row["generation"] if row else 0

    def guard_in_txn(self, generation: int) -> None:
        """Assert the caller still owns the current generation. Call inside a txn."""
        row = self._row()
        current = row["generation"] if row else 0
        if generation != current:
            raise FencedError(
                f"stale generation {generation}; current is {current}"
            )
