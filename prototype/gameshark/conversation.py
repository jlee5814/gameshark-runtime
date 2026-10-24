"""Surface writes into agent conversations, fenced by presence and deduplicated.

A surface sends a user message with a client chosen client_msg_id and the presence
generation it holds. One transaction checks the owner lease, returns the earlier result
for a resent id, rejects a stale presence generation, then appends at next_seq. Rows land
in the same messages table the inference journal uses, so a surface message is ordinary
context for the next model call and the assistant reply still commits only through the
journal.

A resend must carry the original conversation and text; a reused id with other text is
rejected, never acknowledged as the old message.

A dedup hit wins over the presence check: a message that landed before a jack stays
landed, and its resend returns the same seq instead of StalePresence.

Reconnect resync is since(conversation_id, last_seq): every row after the last seq the
surface rendered, in order, with no gap and no duplicate.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from gameshark.clock import Clock
from gameshark.inference.types import InvalidRequest, OwnershipMismatch, Role
from gameshark.lease import LeaseManager
from gameshark.loadout import infer_in_txn
from gameshark.presence import PresenceManager
from gameshark.store import write_txn


@dataclass(frozen=True)
class Submitted:
    conversation_id: str
    seq: int
    replayed: bool


@dataclass(frozen=True)
class Row:
    seq: int
    role: str
    content: str


class SurfaceConversation:
    def __init__(self, conn: sqlite3.Connection, agent_id: str, lease: LeaseManager,
                 generation: int, presence: PresenceManager, clock: Clock,
                 crash: Callable[[str], None] | None = None) -> None:
        self.conn = conn
        self.agent_id = agent_id
        self.lease = lease
        self.generation = generation
        self.presence = presence
        self.clock = clock
        self._crash = crash or (lambda _label: None)

    def submit(self, surface: str, presence_gen: int, client_msg_id: str,
               conversation_id: str, content: str) -> Submitted:
        now = self.clock.now()
        with write_txn(self.conn):
            self.lease.guard_in_txn(self.generation)
            seen = self.conn.execute(
                "SELECT w.conversation_id, w.seq, m.content FROM surface_writes w "
                "LEFT JOIN messages m ON m.conversation_id = w.conversation_id "
                "AND m.seq = w.seq WHERE w.agent_id=? AND w.client_msg_id=?",
                (self.agent_id, client_msg_id),
            ).fetchone()
            if seen is not None:
                if seen["conversation_id"] != conversation_id:
                    raise InvalidRequest(f"{client_msg_id} already names another conversation")
                if seen["content"] != content:
                    raise InvalidRequest(f"{client_msg_id} was already sent with other text")
                return Submitted(conversation_id, seen["seq"], replayed=True)
            self.presence.guard_surface_in_txn(surface, presence_gen)
            conv = self.conn.execute(
                "SELECT agent_id, next_seq FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if conv is None:
                raise InvalidRequest(f"conversation {conversation_id} does not exist")
            if conv["agent_id"] != self.agent_id:
                raise OwnershipMismatch(f"conversation {conversation_id} belongs to another agent")
            seq = conv["next_seq"]
            self.conn.execute(
                "INSERT INTO messages(conversation_id, seq, role, content, call_id, generation, "
                "created) VALUES(?,?,?,?,NULL,?,?)",
                (conversation_id, seq, Role.USER.value, content, self.generation, now))
            self.conn.execute("UPDATE conversations SET next_seq=?, updated=? WHERE id=?",
                              (seq + 1, now, conversation_id))
            # Phase 4.4B: an explicit "I'm at the gym" lands with its row, never twice.
            infer_in_txn(self.conn, self.agent_id, seq, content, self.generation, now)
            self.conn.execute(
                "INSERT INTO surface_writes(agent_id, client_msg_id, surface, presence_gen, "
                "conversation_id, seq, created) VALUES(?,?,?,?,?,?,?)",
                (self.agent_id, client_msg_id, surface, presence_gen, conversation_id, seq, now))
        self._crash("S1_after_submit")
        return Submitted(conversation_id, seq, replayed=False)

    def since(self, conversation_id: str, last_seq: int) -> list[Row]:
        """Rows after last_seq in order. Pass -1 for the whole conversation."""
        rows = self.conn.execute(
            "SELECT m.seq, m.role, m.content FROM messages m "
            "JOIN conversations c ON c.id = m.conversation_id "
            "WHERE m.conversation_id=? AND c.agent_id=? AND m.seq>? ORDER BY m.seq",
            (conversation_id, self.agent_id, last_seq),
        ).fetchall()
        return [Row(r["seq"], r["role"], r["content"]) for r in rows]
