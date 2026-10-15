"""Surface API: the coordinator side of the gateway.

Runs on the thread that owns the supervisor's SQLite connection. pump() answers every
queued request, ticks presence deadlines, and pushes what changed to each live session.
The command set is a fixed table of typed handlers; there is no generic dispatch, no
shell, and no path from a frame to code. A frame type outside the table is refused.

Every mutating command carries the presence generation the surface holds. The surface is
the session's, bound at hello, never a field of the frame. The write and its presence
guard run in one transaction (SurfaceConversation.submit, PresenceManager.ready_out and
ack_in), so a surface jacked out a moment ago writes nothing.

Pushes are at least once: a session that has synced receives rows after the last seq it
was sent, plus presence, responsibility, and activity changes. The surface merges by seq,
so a repeat is harmless and a gap triggers a sync.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from gameshark.errors import Cancelled, Draining, FencedError, HandoffConflict, StalePresence
from gameshark.gateway import HELLO, LOST, PROTO_VERSIONS, GatewayServer, Session
from gameshark.inference.types import InvalidRequest, OwnershipMismatch
from gameshark.stark_chat import CONVERSATION, StarkChatAdapter
from gameshark.supervisor import AgentSupervisor

ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MAX_TEXT = 8 * 1024
PAGE = 500
SERVER = "gameshark-runtime/0.4"
CONVERSATIONS = frozenset({CONVERSATION})   # the only conversations a surface may touch


class Invalid(Exception):
    """A request failed schema validation."""


def _id(msg: dict, key: str) -> str:
    v = msg.get(key)
    if not isinstance(v, str) or not ID_RE.match(v):
        raise Invalid(f"{key} must match {ID_RE.pattern}")
    return v


def _int(msg: dict, key: str, lo: int) -> int:
    v = msg.get(key)
    if not isinstance(v, int) or isinstance(v, bool) or v < lo or v > 2**53:
        raise Invalid(f"{key} must be an integer >= {lo}")
    return v


def _text(msg: dict, key: str, required: bool = True) -> str | None:
    v = msg.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str) or len(v.encode()) > MAX_TEXT or (required and not v.strip()):
        raise Invalid(f"{key} must be non empty text of at most {MAX_TEXT} bytes")
    return v


def _conversation(msg: dict) -> str:
    cid = _id(msg, "conversation_id")
    if cid not in CONVERSATIONS:
        raise Invalid(f"surfaces reach only {sorted(CONVERSATIONS)}")
    return cid


class SurfaceAPI:
    def __init__(self, sup: AgentSupervisor, server: GatewayServer,
                 chat: StarkChatAdapter, log: Callable[[str], None] = lambda _m: None) -> None:
        if sup.presence is None or sup.surface is None:
            raise ValueError("the surface API needs enable_presence before start")
        self.sup = sup
        self.server = server
        self.chat = chat
        self.log = log
        self.rev = 0
        self._last: dict[str, object] = {}
        self.handlers: dict[str, Callable[[Session, dict], dict]] = {
            "ping": self._ping,
            "snapshot": self._snapshot,
            "sync": self._sync,
            "send": self._send,
            "ready_out": self._ready_out,
            "ack_in": self._ack_in,
        }

    # ------------------------------------------------------------------ loop hooks
    def pump(self) -> int:
        """Answer queued requests, apply presence deadlines, push changes."""
        presence = self.sup.presence
        assert presence is not None
        handled = 0
        for sess, msg in self.server.take():
            handled += 1
            if msg is HELLO:
                self.log(f"surface {sess.surface} connected as {sess.id}")
                continue
            if msg is LOST:
                self._lost(sess)
                continue
            self._answer(sess, msg)
        presence.tick()
        self._push_changes()
        return handled

    def next_due(self) -> float | None:
        assert self.sup.presence is not None
        return self.sup.presence.next_deadline()

    def close(self) -> None:
        self.server.close()

    # ------------------------------------------------------------------ dispatch
    def _answer(self, sess: Session, msg: dict) -> None:
        rid = msg.get("id")
        if not isinstance(rid, int) or isinstance(rid, bool):
            sess.send({"type": "error", "id": None, "code": "invalid",
                       "message": "every request carries an integer id"})
            return
        handler = self.handlers.get(msg["type"])
        if handler is None:
            sess.send({"type": "error", "id": rid, "code": "unknown_command",
                       "message": f"{msg['type']!r} is not a gateway command"})
            return
        try:
            data = handler(sess, msg)
        except (Invalid, InvalidRequest, OwnershipMismatch, LookupError, ValueError) as e:
            self._error(sess, rid, "invalid", e)
        except StalePresence as e:
            self._error(sess, rid, "stale_presence", e)
        except HandoffConflict as e:
            self._error(sess, rid, "handoff_conflict", e)
        except Draining as e:
            self._error(sess, rid, "draining", e)
        except (FencedError, Cancelled) as e:
            self._error(sess, rid, "fenced" if isinstance(e, FencedError) else "draining", e)
            raise                             # the owner lost its lease or is stopping
        else:
            sess.send({"type": "result", "id": rid, "data": data})

    @staticmethod
    def _error(sess: Session, rid: int, code: str, e: Exception) -> None:
        sess.send({"type": "error", "id": rid, "code": code, "message": str(e)})

    # ------------------------------------------------------------------ reads
    def _ping(self, sess: Session, msg: dict) -> dict:
        return {"pong": True, "rev": self.rev}

    def _snapshot(self, sess: Session, msg: dict) -> dict:
        return self.snapshot(sess.surface)

    def snapshot(self, surface: str) -> dict:
        sup = self.sup
        ident = sup.identity
        return {
            "server": SERVER, "proto": list(PROTO_VERSIONS),
            "identity": {"agent_id": ident.agent_id, "uuid": ident.uuid,
                         "created": ident.created},
            "owner_generation": sup.generation,
            "you": surface,
            **self._presence_view(),
            "responsibilities": self._responsibilities(),
            "conversation": {"id": CONVERSATION, "head_seq": self._head_seq()},
            "activity": self._activity(),
        }

    def _sync(self, sess: Session, msg: dict) -> dict:
        cid = _conversation(msg)
        after = _int(msg, "after_seq", -1)
        assert self.sup.surface is not None
        rows = self.sup.surface.since(cid, after)
        page, more = rows[:PAGE], len(rows) > PAGE
        if page:
            sess.synced_seq = max(sess.synced_seq or -1, page[-1].seq)
        elif sess.synced_seq is None:
            sess.synced_seq = after
        return {"conversation_id": cid, "rows": [self._row(r) for r in page], "more": more}

    # ------------------------------------------------------------------ writes
    def _send(self, sess: Session, msg: dict) -> dict:
        cid = _conversation(msg)
        client_msg_id = _id(msg, "client_msg_id")
        text = _text(msg, "text")
        gen = _int(msg, "presence_gen", 0)
        assert self.sup.surface is not None
        sub = self.sup.surface.submit(sess.surface, gen, client_msg_id, cid, text)
        self.chat.ensure_reply()
        return {"conversation_id": cid, "seq": sub.seq, "replayed": sub.replayed}

    def _ready_out(self, sess: Session, msg: dict) -> dict:
        hid = _id(msg, "handoff_id")
        draft = _text(msg, "draft", required=False)
        gen = _int(msg, "presence_gen", 0)
        assert self.sup.presence is not None
        h = self.sup.presence.ready_out(hid, draft, surface=sess.surface, presence_gen=gen)
        return {"handoff_id": h.id, "state": h.state, "to_gen": h.to_gen}

    def _ack_in(self, sess: Session, msg: dict) -> dict:
        hid = _id(msg, "handoff_id")
        gen = _int(msg, "presence_gen", 0)
        assert self.sup.presence is not None
        h = self.sup.presence.ack_in(hid, sess.surface, gen)
        return {"handoff_id": h.id, "state": h.state, "draft": h.draft}

    def _lost(self, sess: Session) -> None:
        self.log(f"surface {sess.surface} disconnected ({sess.id})")
        if any(s.surface == sess.surface for s in self.server.live_sessions()):
            return
        assert self.sup.presence is not None
        h = self.sup.presence.surface_lost(sess.surface)
        if h is not None:
            self.log(f"handoff {h.id} {h.state} after {sess.surface} left")

    # ------------------------------------------------------------------ views
    def _presence_view(self) -> dict:
        pm = self.sup.presence
        assert pm is not None
        p = pm.current()
        h = pm.open_handoff()
        return {
            "presence": {"surface": p.surface, "home": p.home, "generation": p.generation},
            "handoff": None if h is None else {
                "id": h.id, "from": h.from_surface, "to": h.to_surface, "state": h.state,
                "from_gen": h.from_gen, "to_gen": h.to_gen,
                "deadline": h.prepare_deadline if h.state == "preparing" else h.ack_deadline},
        }

    def _responsibilities(self) -> list[dict]:
        rows = self.sup.conn.execute(
            "SELECT id, kind, state, checkpoint, updated FROM responsibilities "
            "WHERE agent_id=? ORDER BY (state IN ('active','pending')) DESC, updated DESC "
            "LIMIT 20", (self.sup.agent_id,)).fetchall()
        return [dict(r) for r in rows]

    def _head_seq(self) -> int:
        row = self.sup.conn.execute(
            "SELECT MAX(seq) AS s FROM messages WHERE conversation_id=?",
            (CONVERSATION,)).fetchone()
        return -1 if row["s"] is None else row["s"]

    def _activity(self) -> dict:
        return {"working": self.chat.pending is not None, "call_id": self.chat.pending}

    @staticmethod
    def _row(r) -> dict:
        return {"seq": r.seq, "role": r.role, "content": r.content}

    # ------------------------------------------------------------------ pushes
    def _push(self, sessions: list[Session], kind: str, data: dict) -> None:
        self.rev += 1
        for s in sessions:
            s.send({"type": "event", "kind": kind, "rev": self.rev, "data": data})

    def _changed(self, key: str, value: object) -> bool:
        if self._last.get(key) == value:
            return False
        self._last[key] = value
        return True

    def _push_changes(self) -> None:
        sessions = self.server.live_sessions()
        if not sessions:
            self._last.clear()
            return
        presence = self._presence_view()
        if self._changed("presence", presence):
            self._push(sessions, "presence", presence)
        resp = self._responsibilities()
        if self._changed("responsibilities", resp):
            self._push(sessions, "responsibilities", {"responsibilities": resp})
        head = self._head_seq()
        assert self.sup.surface is not None
        for s in sessions:
            if s.synced_seq is None or s.synced_seq >= head:
                continue
            rows = self.sup.surface.since(CONVERSATION, s.synced_seq)[:PAGE]
            s.synced_seq = rows[-1].seq
            self.rev += 1
            s.send({"type": "event", "kind": "messages", "rev": self.rev,
                    "data": {"conversation_id": CONVERSATION,
                             "rows": [self._row(r) for r in rows]}})
        # After rows, so a surface never shows Stark idle before his reply has arrived.
        activity = self._activity()
        if self._changed("activity", activity):
            self._push(sessions, "activity", activity)
