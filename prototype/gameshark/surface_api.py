"""Surface API: the coordinator side of the gateway.

Runs on the thread that owns the supervisor's SQLite connection. pump() answers every
queued request, ticks presence deadlines, and pushes what changed to each live session.
The command set is a fixed table of typed handlers; there is no generic dispatch, no
shell, and no path from a frame to code. A frame type outside the table is refused.

Every mutating command carries the presence generation the surface holds. The surface is
the session's, bound at hello, never a field of the frame. The write and its presence
guard run in one transaction (SurfaceConversation.submit, PresenceManager.ready_out and
ack_in), so a surface jacked out a moment ago writes nothing.

Paired devices (net_gateway.py) authenticate here too: their first frame arrives as an
AuthRequest, and the device proof or pairing is checked against the database on this
thread. Each pump first applies operator revocations, closing a revoked device's sessions
and evicting it from presence before any of its queued requests could be answered.

A failed reply is retried only by retry_reply, fenced by presence like send. The runtime
never answers in Stark's voice on its own: no fallback text, only the reply state in
activity (queued, thinking, waiting, failed) and the model behind it in the snapshot.

Pushes are at least once: a session that has synced receives rows after the last seq it
was sent, plus presence, responsibility, and activity changes. The surface merges by seq,
so a repeat is harmless and a gap triggers a sync.

Memory (Phase 4.2) is four typed commands: memory_list reads, memory_remember and
memory_correct are fenced by presence like send, and memory_forget only by the lease, so
any authenticated surface may always delete. Each write carries a client chosen op_id; a
resend returns the first result. Saved text never becomes a conversation row. A change
pushes the whole list (at most 256 rows) as a memories event.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable

from gameshark.devices import AuthRejected, DeviceRegistry
from gameshark.errors import Cancelled, Draining, FencedError, HandoffConflict, StalePresence
from gameshark.gateway import HELLO, LOST, PROTO_VERSIONS, GatewayServer, Session
from gameshark.inference.fake import FakeProvider
from gameshark.inference.types import InvalidRequest, OwnershipMismatch
from gameshark.memory import ID_RE as MEMORY_ID_RE
from gameshark.memory import MAX_TEXT as MEMORY_TEXT
from gameshark.memory import MemoryConflict, MemoryLimit
from gameshark.net_gateway import AuthRequest, TLSSession
from gameshark.presence import ACKED, COMMITTED
from gameshark.stark_chat import CONVERSATION, StarkChatAdapter
from gameshark.store import write_txn
from gameshark.supervisor import AgentSupervisor

ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MAX_TEXT = 8 * 1024
PAGE = 500
SERVER = "gameshark-runtime/0.4"
CONVERSATIONS = frozenset({CONVERSATION})   # the only conversations a surface may touch
FEATURES = ("memory",)                      # optional commands this runtime serves


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


def _hex(msg: dict, key: str, max_bytes: int = 128) -> bytes:
    v = msg.get(key)
    if not isinstance(v, str) or len(v) > 2 * max_bytes:
        raise Invalid(f"{key} must be hex of at most {max_bytes} bytes")
    try:
        return bytes.fromhex(v)
    except ValueError:
        raise Invalid(f"{key} must be hex") from None


def _memory_text(msg: dict) -> str:
    v = msg.get("text")
    if not isinstance(v, str) or not v.strip() or len(v.encode()) > 4 * MEMORY_TEXT:
        raise Invalid(f"text must be non empty text of at most {MEMORY_TEXT} bytes")
    return v                                  # MemoryStore normalizes and bounds it exactly


def _memory_id(msg: dict) -> str:
    v = msg.get("memory_id")
    if not isinstance(v, str) or not MEMORY_ID_RE.match(v):
        raise Invalid(f"memory_id must match {MEMORY_ID_RE.pattern}")
    return v


def _device(sess: Session) -> str | None:
    return getattr(sess, "device_id", None)


def _conversation(msg: dict) -> str:
    cid = _id(msg, "conversation_id")
    if cid not in CONVERSATIONS:
        raise Invalid(f"surfaces reach only {sorted(CONVERSATIONS)}")
    return cid


class SurfaceAPI:
    def __init__(self, sup: AgentSupervisor, server: GatewayServer,
                 chat: StarkChatAdapter, log: Callable[[str], None] = lambda _m: None,
                 devices: DeviceRegistry | None = None) -> None:
        if sup.presence is None or sup.surface is None:
            raise ValueError("the surface API needs enable_presence before start")
        self.sup = sup
        self.server = server
        self.chat = chat
        self.log = log
        self.devices = devices
        self.rev = 0
        self.boot = str(uuid.uuid4())   # this runtime instance; a restart mints a new one
        self._last: dict[str, object] = {}
        sup.activity.on_begin = self._push_activity
        self.handlers: dict[str, Callable[[Session, dict], dict]] = {
            "ping": self._ping,
            "snapshot": self._snapshot,
            "sync": self._sync,
            "send": self._send,
            "retry_reply": self._retry_reply,
            "ready_out": self._ready_out,
            "ack_in": self._ack_in,
            "jack": self._jack,
            "draft_ack": self._draft_ack,
            "memory_list": self._memory_list,
            "memory_remember": self._memory_remember,
            "memory_correct": self._memory_correct,
            "memory_forget": self._memory_forget,
        }

    # ------------------------------------------------------------------ loop hooks
    def pump(self) -> int:
        """Answer queued requests, apply presence deadlines, push changes."""
        presence = self.sup.presence
        assert presence is not None
        handled = 0
        self._enforce_revocations()
        for sess, msg in self.server.take():
            handled += 1
            if isinstance(msg, AuthRequest):
                self._authenticate(sess, msg)
                continue
            if sess.revoked:
                continue
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
        except MemoryConflict as e:
            self._error(sess, rid, "conflict", e)
        except MemoryLimit as e:
            self._error(sess, rid, "memory_limit", e)
        except PermissionError as e:
            self._error(sess, rid, "forbidden", e)
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
            # The model behind Stark's replies: runtime configuration, never his identity.
            "model": self._model(),
            # A draft carried here by an acked jack, until this surface confirms it.
            "draft": self._draft_view(surface),
            # Display names of paired devices, so a surface can say where Stark is.
            "devices": [{"surface": k, "name": v} for k, v in
                        sorted((self.devices.names() if self.devices else {}).items())],
            "features": list(FEATURES),
            # Counts only; the list itself comes from memory_list and memories pushes.
            "memory": self._memory_summary(),
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

    def _retry_reply(self, sess: Session, msg: dict) -> dict:
        cid = _conversation(msg)
        seq = _int(msg, "seq", 0)
        gen = _int(msg, "presence_gen", 0)
        sup = self.sup
        assert sup.presence is not None
        with write_txn(sup.conn):
            sup.lease.guard_in_txn(sup.generation)
            sup.presence.guard_surface_in_txn(sess.surface, gen)
        retry, replayed = self.chat.retry(seq)
        return {"conversation_id": cid, "seq": seq, "retry": retry, "replayed": replayed}

    def _ready_out(self, sess: Session, msg: dict) -> dict:
        hid = _id(msg, "handoff_id")
        draft = _text(msg, "draft", required=False)
        gen = _int(msg, "presence_gen", 0)
        assert self.sup.presence is not None
        h = self.sup.presence.ready_out(hid, sess.surface, gen, draft)
        # The source's receipt: the committed row holds exactly this text and the jack has
        # not reverted (a reverted row keeps its draft only for recovery). A replay asks
        # again, so a lost reply, a lost request, or a restart all reconcile the same way.
        # The source clears its composer only once the state is acked.
        carried = h.state in (COMMITTED, ACKED) and h.draft is not None and h.draft == draft
        return {"handoff_id": h.id, "state": h.state, "to_gen": h.to_gen,
                "draft_carried": carried}

    def _ack_in(self, sess: Session, msg: dict) -> dict:
        hid = _id(msg, "handoff_id")
        gen = _int(msg, "presence_gen", 0)
        assert self.sup.presence is not None
        h = self.sup.presence.ack_in(hid, sess.surface, gen)
        # The draft rides along only while it awaits receipt; a confirmed one never returns.
        draft = h.draft if h.draft_state == "pending" else None
        return {"handoff_id": h.id, "state": h.state, "draft": draft}

    def _draft_ack(self, sess: Session, msg: dict) -> dict:
        hid = _id(msg, "handoff_id")
        gen = _int(msg, "presence_gen", 0)
        assert self.sup.presence is not None
        h = self.sup.presence.draft_ack(hid, sess.surface, gen)
        return {"handoff_id": h.id, "draft_state": h.draft_state}

    def _jack(self, sess: Session, msg: dict) -> dict:
        hid = _id(msg, "handoff_id")
        to = msg.get("to")
        if to not in ("self", "home"):
            raise Invalid("to must be self or home")
        gen = _int(msg, "presence_gen", 0)
        assert self.sup.presence is not None
        h = self.sup.presence.request_jack_by(hid, sess.surface, gen, to)
        return {"handoff_id": h.id, "state": h.state, "from": h.from_surface,
                "to": h.to_surface}

    # ------------------------------------------------------------------ memory
    def _memory_list(self, sess: Session, msg: dict) -> dict:
        return self._memories()

    def _memory_remember(self, sess: Session, msg: dict) -> dict:
        op = _id(msg, "op_id")
        text = _memory_text(msg)
        gen = _int(msg, "presence_gen", 0)
        m = self._store().remember(sess.surface, gen, op, text, device_id=_device(sess))
        return {"memory": m.view(), "replayed": m.replayed}

    def _memory_correct(self, sess: Session, msg: dict) -> dict:
        op = _id(msg, "op_id")
        mid = _memory_id(msg)
        rev = _int(msg, "revision", 1)
        text = _memory_text(msg)
        gen = _int(msg, "presence_gen", 0)
        m = self._store().correct(sess.surface, gen, op, mid, rev, text,
                                  device_id=_device(sess))
        return {"memory": m.view(), "replayed": m.replayed}

    def _memory_forget(self, sess: Session, msg: dict) -> dict:
        op = _id(msg, "op_id")
        mid = _memory_id(msg)
        m = self._store().forget(sess.surface, op, mid, device_id=_device(sess))
        # Saved memory is gone; conversation rows that quote it are not. Say how many.
        return {"memory_id": m.id, "state": m.state, "replayed": m.replayed,
                "history_mentions": m.history_mentions}

    def _store(self):
        if self.sup.memory is None:
            raise Invalid("memory is not enabled")
        return self.sup.memory

    def _memories(self) -> dict:
        store = self._store()
        return {"memories": [m.view() for m in store.list()],
                "inject": store.inject_enabled()}

    def _memory_summary(self) -> dict:
        store = self._store()
        return {"count": store.count(), "inject": store.inject_enabled()}

    # ------------------------------------------------------------------ devices
    def _authenticate(self, sess: Session, req: AuthRequest) -> None:
        assert isinstance(sess, TLSSession)
        f = req.frame
        proto = f.get("proto")
        if not isinstance(proto, int) or isinstance(proto, bool) or proto not in PROTO_VERSIONS:
            sess.rejected("auth", "unsupported protocol version")
            sess.reject("unsupported_version", f"server speaks {list(PROTO_VERSIONS)}")
            return
        try:
            if self.devices is None:
                raise AuthRejected("device pairing is not enabled")
            if req.kind == "pair":
                secret = f.get("secret")
                if not isinstance(secret, str) or not 0 < len(secret) <= 64:
                    raise Invalid("secret must be 1 to 64 characters")
                d = self.devices.complete_pairing(_id(f, "pairing_id"), secret,
                                                  _hex(f, "public_key"), f.get("name"))
                self.log(f"paired device {d.id} ({d.name}) as {d.surface} from {sess.peer}")
                sess.send({"type": "paired", "device_id": d.id, "surface": d.surface,
                           "identity": self.server.identity})
                sess.close()
                return
            d = self.devices.authenticate(_id(f, "device_id"), req.nonce, proto,
                                          req.server_fp, _hex(f, "sig"))
        except (AuthRejected, Invalid) as e:
            sess.rejected("auth", str(e))       # ids and reasons; never the secret or proof
            sess.reject("auth_failed", "authentication failed")
            return
        except FencedError:
            sess.reject("fenced", "runtime lost ownership")
            raise
        sess.accept(d.surface, d.id, proto, {
            "type": "welcome", "proto": proto, "session": sess.id, "surface": d.surface,
            "device_id": d.id, "identity": self.server.identity})
        self.log(f"device {d.name} connected as {d.surface} via {sess.path} from "
                 f"{sess.peer} ({sess.id})")
        # A phone that changed networks proves itself again before its old socket dies.
        # The newest session wins; it is live first, so losing the old one never counts
        # as the surface leaving, and presence stays where it is.
        for old in self.server.live_sessions():
            if (old is not sess and isinstance(old, TLSSession)
                    and old.device_id == d.id and not old.closed):
                old.supersede()
                self.log(f"closed {old.id}: {d.surface} moved to {sess.path} ({sess.id})")

    def _enforce_revocations(self) -> None:
        """Close revoked devices' sessions and take presence back from them."""
        if self.devices is None:
            return
        revoked = self.devices.revoked_surfaces()
        if not revoked:
            return
        for s in self.server.live_sessions():
            if s.surface in revoked:
                s.revoked = True
                s.close()
                self.log(f"closed {s.id}: device {s.surface} revoked")
        pm = self.sup.presence
        assert pm is not None
        h = pm.open_handoff()
        involved = {pm.current().surface} | ({h.from_surface, h.to_surface} if h else set())
        for surface in involved & revoked:
            if pm.evict(surface):
                self.log(f"evicted revoked {surface} from presence")

    def _lost(self, sess: Session) -> None:
        self.log(f"surface {sess.surface} disconnected ({sess.id})")
        if any(s.surface == sess.surface for s in self.server.live_sessions()):
            return
        assert self.sup.presence is not None
        h = self.sup.presence.surface_lost(sess.surface)
        if h is not None:
            self.log(f"handoff {h.id} {h.state} after {sess.surface} left")

    # ------------------------------------------------------------------ views
    def _draft_view(self, surface: str) -> dict | None:
        pm = self.sup.presence
        assert pm is not None
        h = pm.pending_draft(surface, pm.current().generation)
        return None if h is None else {"handoff_id": h.id, "text": h.draft}

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

    def _model(self) -> dict | None:
        router = self.sup.inference_router
        if router is None or not router.providers:
            return None
        provider = next(iter(router.providers.values())).provider
        caps = provider.capabilities()
        return {"provider": provider.name, "model": caps.model,
                "locality": caps.locality.value, "fake": isinstance(provider, FakeProvider)}

    def _head_seq(self) -> int:
        row = self.sup.conn.execute(
            "SELECT MAX(seq) AS s FROM messages WHERE conversation_id=?",
            (CONVERSATION,)).fetchone()
        return -1 if row["s"] is None else row["s"]

    def _activity(self) -> dict:
        blocked = [r["id"] for r in self.sup.conn.execute(
            "SELECT id FROM responsibilities WHERE agent_id=? AND state='blocked'",
            (self.sup.agent_id,))]
        return {**self.sup.activity.view(blocked), "boot": self.boot,
                "owner_generation": self.sup.generation,
                # v1 fields: the companion reply in flight.
                "working": self.chat.pending is not None, "call_id": self.chat.pending,
                # Phase 4.1: the head reply's state, absent once it is answered.
                "reply": self.chat.reply_view(self.sup.activity.calls())}

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
        if self.sup.memory is not None and self._changed("memories", self.sup.memory.version):
            self._push(sessions, "memories", self._memories())
        # After rows, so a surface never shows Stark idle before his reply has arrived.
        self._push_activity(sessions)

    def _push_activity(self, sessions: list[Session] | None = None) -> None:
        """Push activity if it changed. Called at once when work begins, since that work
        may hold the coordinator; the end waits for the pump, after any new rows."""
        sessions = self.server.live_sessions() if sessions is None else sessions
        if not sessions:
            return
        activity = self._activity()
        if self._changed("activity", activity):
            self._push(sessions, "activity", activity)
