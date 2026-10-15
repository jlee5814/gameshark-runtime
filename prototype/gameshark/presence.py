"""Presence: which surface is Stark's active body, and the jack between surfaces.

The agent never moves. The runtime keeps identity, duties, and conversations on the host;
a surface (the Mac panel, a paired phone) is a view. Presence names the one surface that
may write as the active body, under a monotonic generation. Every surface write carries
the generation it saw, and guard_surface_in_txn rejects a stale one, so a jacked out
surface can render but never act (P2).

A jack is a two phase handoff, every transition one fenced transaction:

    preparing --ready_out | prepare deadline--> committed (generation + 1, surface = target)
    preparing --target lost--------------------> aborted   (presence unchanged)
    committed --ack_in-------------------------> acked
    committed --ack deadline | target lost-----> reverted  (generation + 1, surface = home,
                                                            or source when home failed)

Deadlines persist as absolute times, so a runtime killed mid jack resolves the same way
after restart: tick() finishes whatever the dead process left open (P3). At most one
handoff is open at a time, and presence always names exactly one surface (P1).

Crash points, fired through the supervisor's crash hook:

    P1_after_prepare   handoff row is preparing; restart resumes it
    P2_after_commit    target holds presence unacked; restart awaits ack or reverts
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from gameshark.clock import Clock
from gameshark.errors import HandoffConflict, StalePresence
from gameshark.lease import LeaseManager
from gameshark.store import write_txn

PREPARING, COMMITTED, ACKED, REVERTED, ABORTED = (
    "preparing", "committed", "acked", "reverted", "aborted")
OPEN = (PREPARING, COMMITTED)

PREPARE_TIMEOUT_S = 3.0   # source plays its exit and flushes its draft
ACK_TIMEOUT_S = 10.0      # target plays its arrival and confirms


@dataclass(frozen=True)
class Presence:
    surface: str
    home: str
    generation: int


@dataclass(frozen=True)
class Handoff:
    id: str
    from_surface: str
    to_surface: str
    from_gen: int
    to_gen: int | None
    state: str
    draft: str | None
    prepare_deadline: float
    ack_deadline: float | None

    @classmethod
    def of(cls, row: sqlite3.Row) -> Handoff:
        return cls(row["id"], row["from_surface"], row["to_surface"], row["from_gen"],
                   row["to_gen"], row["state"], row["draft"], row["prepare_deadline"],
                   row["ack_deadline"])


class PresenceManager:
    def __init__(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        lease: LeaseManager,
        generation: int,
        clock: Clock,
        crash: Callable[[str], None] | None = None,
        prepare_timeout_s: float = PREPARE_TIMEOUT_S,
        ack_timeout_s: float = ACK_TIMEOUT_S,
    ) -> None:
        self.conn = conn
        self.agent_id = agent_id
        self.lease = lease
        self.generation = generation
        self.clock = clock
        self._crash = crash or (lambda _label: None)
        self.prepare_timeout_s = prepare_timeout_s
        self.ack_timeout_s = ack_timeout_s

    # ------------------------------------------------------------------ reads
    def _fenced(self, fn: Callable[[sqlite3.Connection], object]) -> object:
        with write_txn(self.conn):
            self.lease.guard_in_txn(self.generation)
            return fn(self.conn)

    def _presence(self) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM presence WHERE agent_id=?", (self.agent_id,)).fetchone()
        if row is None:
            raise LookupError(f"{self.agent_id} has no presence; call ensure_home first")
        return row

    def current(self) -> Presence:
        row = self._presence()
        return Presence(row["surface"], row["home"], row["generation"])

    def handoff(self, handoff_id: str) -> Handoff | None:
        row = self.conn.execute(
            "SELECT * FROM handoffs WHERE id=? AND agent_id=?", (handoff_id, self.agent_id)
        ).fetchone()
        return Handoff.of(row) if row else None

    def open_handoff(self) -> Handoff | None:
        row = self.conn.execute(
            "SELECT * FROM handoffs WHERE agent_id=? AND state IN (?,?)",
            (self.agent_id, *OPEN),
        ).fetchone()
        return Handoff.of(row) if row else None

    # ------------------------------------------------------------------ guards
    def guard_surface_in_txn(self, surface: str, presence_gen: int) -> None:
        """Assert surface is the active body at presence_gen. Call inside a write txn."""
        row = self._presence()
        if row["surface"] != surface or row["generation"] != presence_gen:
            raise StalePresence(
                f"{surface}@{presence_gen} is stale; active is "
                f"{row['surface']}@{row['generation']}")

    # ------------------------------------------------------------------ transitions
    def ensure_home(self, home: str) -> Presence:
        """Create presence once at its home surface. Restart keeps the existing row."""
        now = self.clock.now()

        def body(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR IGNORE INTO presence(agent_id, surface, home, generation, updated) "
                "VALUES(?,?,?,1,?)", (self.agent_id, home, home, now))

        self._fenced(body)
        return self.current()

    def request_jack(self, handoff_id: str, to_surface: str) -> Handoff:
        """Open a jack from the active surface to to_surface. Idempotent for one id."""
        now = self.clock.now()

        def body(conn: sqlite3.Connection) -> Handoff:
            existing = conn.execute(
                "SELECT * FROM handoffs WHERE id=?", (handoff_id,)).fetchone()
            if existing is not None:
                if existing["agent_id"] != self.agent_id or existing["to_surface"] != to_surface:
                    raise HandoffConflict(f"handoff id {handoff_id} names another jack")
                return Handoff.of(existing)
            other = self.open_handoff()
            if other is not None:
                raise HandoffConflict(f"handoff {other.id} is still {other.state}")
            p = self._presence()
            if p["surface"] == to_surface:
                raise HandoffConflict(f"{to_surface} already holds presence")
            conn.execute(
                "INSERT INTO handoffs(id, agent_id, from_surface, to_surface, from_gen, "
                "to_gen, state, draft, prepare_deadline, ack_deadline, created, updated) "
                "VALUES(?,?,?,?,?,NULL,?,NULL,?,NULL,?,?)",
                (handoff_id, self.agent_id, p["surface"], to_surface, p["generation"],
                 PREPARING, now + self.prepare_timeout_s, now, now))
            return Handoff.of(conn.execute(
                "SELECT * FROM handoffs WHERE id=?", (handoff_id,)).fetchone())

        h = self._fenced(body)
        self._crash("P1_after_prepare")
        return h

    def ready_out(self, handoff_id: str, draft: str | None = None) -> Handoff:
        """Source finished its exit: move presence to the target. Idempotent after commit."""
        h = self._commit(handoff_id, draft, carry_draft=True)
        self._crash("P2_after_commit")
        return h

    def _commit(self, handoff_id: str, draft: str | None, carry_draft: bool) -> Handoff:
        now = self.clock.now()

        def body(conn: sqlite3.Connection) -> Handoff:
            row = self._handoff_row(handoff_id)
            if row["state"] != PREPARING:
                return Handoff.of(row)          # already committed, acked, or resolved
            p = self._presence()
            to_gen = p["generation"] + 1
            conn.execute("UPDATE presence SET surface=?, generation=?, updated=? "
                         "WHERE agent_id=?", (row["to_surface"], to_gen, now, self.agent_id))
            conn.execute(
                "UPDATE handoffs SET state=?, to_gen=?, draft=?, ack_deadline=?, updated=? "
                "WHERE id=?",
                (COMMITTED, to_gen, draft if carry_draft else None,
                 now + self.ack_timeout_s, now, handoff_id))
            return Handoff.of(self._handoff_row(handoff_id))

        return self._fenced(body)

    def ack_in(self, handoff_id: str, surface: str, presence_gen: int) -> Handoff:
        """Target finished its arrival. Rejects a target that already lost presence."""
        now = self.clock.now()

        def body(conn: sqlite3.Connection) -> Handoff:
            row = self._handoff_row(handoff_id)
            if row["state"] == ACKED and row["to_gen"] == presence_gen:
                return Handoff.of(row)
            if row["state"] != COMMITTED or row["to_surface"] != surface:
                raise StalePresence(f"handoff {handoff_id} is {row['state']}, not awaiting "
                                    f"{surface}")
            self.guard_surface_in_txn(surface, presence_gen)
            conn.execute("UPDATE handoffs SET state=?, updated=? WHERE id=?",
                         (ACKED, now, handoff_id))
            return Handoff.of(self._handoff_row(handoff_id))

        return self._fenced(body)

    def surface_lost(self, surface: str) -> Handoff | None:
        """A surface disconnected. Abort or revert an open jack that targets it."""
        h = self.open_handoff()
        if h is None or h.to_surface != surface:
            return None
        return self._resolve(h.id, lost=True)

    def tick(self) -> Handoff | None:
        """Apply any passed deadline to the open handoff. Safe to call on every loop."""
        h = self.open_handoff()
        if h is None:
            return None
        now = self.clock.now()
        if h.state == PREPARING and now >= h.prepare_deadline:
            # The source never confirmed; it may be dead. The target is live, so commit
            # without a draft rather than strand the jack.
            return self._commit(h.id, None, carry_draft=False)
        if h.state == COMMITTED and h.ack_deadline is not None and now >= h.ack_deadline:
            return self._resolve(h.id, lost=False)
        return None

    def _resolve(self, handoff_id: str, lost: bool) -> Handoff:
        now = self.clock.now()

        def body(conn: sqlite3.Connection) -> Handoff:
            row = self._handoff_row(handoff_id)
            if row["state"] == PREPARING and lost:
                conn.execute("UPDATE handoffs SET state=?, updated=? WHERE id=?",
                             (ABORTED, now, handoff_id))
            elif row["state"] == COMMITTED:
                p = self._presence()
                # Fall back home, unless home is the target that just failed.
                back = row["from_surface"] if row["to_surface"] == p["home"] else p["home"]
                conn.execute("UPDATE presence SET surface=?, generation=?, updated=? "
                             "WHERE agent_id=?",
                             (back, p["generation"] + 1, now, self.agent_id))
                conn.execute("UPDATE handoffs SET state=?, updated=? WHERE id=?",
                             (REVERTED, now, handoff_id))
            return Handoff.of(self._handoff_row(handoff_id))

        return self._fenced(body)

    def _handoff_row(self, handoff_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM handoffs WHERE id=? AND agent_id=?", (handoff_id, self.agent_id)
        ).fetchone()
        if row is None:
            raise LookupError(f"no handoff {handoff_id}")
        return row
