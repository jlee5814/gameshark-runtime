"""Exclusive ownership with fencing.

A lease carries a monotonic generation. Acquiring a free or expired lease bumps the
generation. Every durable commit is guarded against the current generation, so a
stale holder whose lease was taken over cannot commit (invariant I2, fencing).

A lease moves through three states:

    held --begin_drain--> draining --release--> released --acquire--> held (generation + 1)

Draining keeps the lease live: the holder admits no new work, keeps renewing while in
flight work finishes, and a successor stays refused until release or expiry. Release
keeps the generation, and the guard rejects any write under a released lease, so the old
generation can never commit again even before a successor appears. An expired lease
refuses renewal, so its holder learns of the loss at its next heartbeat.
"""

from __future__ import annotations

import sqlite3

from gameshark.clock import Clock
from gameshark.errors import FencedError, LeaseDenied
from gameshark.store import write_txn

HELD, DRAINING, RELEASED = "held", "draining", "released"


class LeaseManager:
    def __init__(self, conn: sqlite3.Connection, agent_id: str, clock: Clock) -> None:
        self.conn = conn
        self.agent_id = agent_id
        self.clock = clock

    def _row(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT owner_token, generation, expires_at, state FROM lease WHERE agent_id=?",
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
                    "INSERT INTO lease(agent_id, owner_token, generation, expires_at, state) "
                    "VALUES(?,?,?,?,?)",
                    (self.agent_id, owner_token, gen, now + ttl_s, HELD),
                )
                return gen

            live = row["expires_at"] > now
            same_owner = row["owner_token"] == owner_token
            if live and not same_owner:
                # A draining lease is still live: its holder is finishing in flight work.
                raise LeaseDenied(
                    f"lease on {self.agent_id} held by another owner until {row['expires_at']}"
                )

            # Free, expired, or ours: take or renew. Reclaiming our own draining lease means
            # the draining incarnation died (the instance lock proves it), so fence it off.
            if same_owner and live and row["state"] == HELD:
                gen = row["generation"]  # renew keeps the generation
            else:
                gen = row["generation"] + 1  # takeover bumps the fence
            self.conn.execute(
                "UPDATE lease SET owner_token=?, generation=?, expires_at=?, state=? "
                "WHERE agent_id=?",
                (owner_token, gen, now + ttl_s, HELD, self.agent_id),
            )
            return gen

    def _require_owned(self, row: sqlite3.Row | None, owner_token: str, generation: int,
                       action: str) -> sqlite3.Row:
        if row is None or row["owner_token"] != owner_token or row["generation"] != generation:
            raise FencedError(f"cannot {action} a lease we no longer hold")
        if row["expires_at"] <= self.clock.now():
            raise FencedError(f"cannot {action} an expired lease")
        return row

    def renew(self, owner_token: str, generation: int, ttl_s: float) -> None:
        """Extend our lease, held or draining. Raise FencedError once it is lost.

        Lost means another owner took it over, we released it, or it expired. The caller
        must stop serving on FencedError: retrying can never succeed for this generation.
        """
        now = self.clock.now()
        with write_txn(self.conn):
            self._require_owned(self._row(), owner_token, generation, "renew")
            self.conn.execute(
                "UPDATE lease SET expires_at=? WHERE agent_id=?", (now + ttl_s, self.agent_id)
            )

    def begin_drain(self, owner_token: str, generation: int) -> None:
        """Mark our live lease draining. Idempotent. Raise FencedError once it is lost."""
        with write_txn(self.conn):
            self._require_owned(self._row(), owner_token, generation, "drain")
            self.conn.execute(
                "UPDATE lease SET state=? WHERE agent_id=?", (DRAINING, self.agent_id)
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
                    "UPDATE lease SET owner_token=NULL, expires_at=0, state=? WHERE agent_id=?",
                    (RELEASED, self.agent_id),
                )

    def current_generation(self) -> int:
        row = self._row()
        return row["generation"] if row else 0

    def state(self) -> str | None:
        row = self._row()
        return row["state"] if row else None

    def guard_in_txn(self, generation: int) -> None:
        """Assert the caller still owns the current generation. Call inside a txn.

        A released lease fences its own generation too, so nothing from the old owner
        commits between its release and a successor's acquire.
        """
        row = self._row()
        current = row["generation"] if row else 0
        if generation != current:
            raise FencedError(
                f"stale generation {generation}; current is {current}"
            )
        if row is not None and row["state"] == RELEASED:
            raise FencedError(f"generation {generation} released its lease")
