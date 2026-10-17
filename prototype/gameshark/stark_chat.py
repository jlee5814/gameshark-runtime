"""Stark's companion conversation: surface messages in, inference replies out.

A surface writes user rows into the one companion conversation through
SurfaceConversation (fenced, deduplicated). This adapter answers the newest unanswered
user row with one inference call; the journal commits the assistant row, and the surface
renders it from the same table. There is no external effect: the conversation is the
delivery.

One reply event per user seq, event id ev-reply-stark:main-<seq>, so the inbox dedups it.
poll() runs every tick and submits the event for an unanswered head row, so a crash
between the user write and the event insert heals on the next tick. A handler whose seq is
no longer the head ends without a call: a newer user row has its own event, and its
reply covers the whole context. A user write that lands while a call runs makes the
journal refuse that commit (ContextConflict, retryable); the redelivered event then finds
itself superseded and ends.

An adapter built with serve=False (a CLI applying controls while no daemon runs) leaves
every reply event pending for the serving daemon instead of starting inference.

A reply that fails terminally (attempts exhausted, auth refused, a context that cannot
fit) writes no text: the event acks, the duty turns blocked with the error kind, and the
head row stays unanswered. That is the durable failure record; reply_view reads it back
after any restart. retry(seq) answers it with a new event and call, numbered r1, r2, ...
per seq, so a resent retry finds its own pending event and never starts a second call.
Retryable refusals (model down, timeout before the attempt cap) only hold the event; the
adapter keeps the latest reason in memory so surfaces can say Stark is waiting and why.
"""

from __future__ import annotations

import math
import time
import uuid as uuidlib
from collections.abc import Callable

from gameshark.adapters import AdapterContext
from gameshark.events import Deferred
from gameshark.inference import (
    CallSpec,
    Capabilities,
    FakeCall,
    FakeProvider,
    Locality,
    Privacy,
    ProviderRequest,
    Role,
    RouteResult,
    Script,
)
from gameshark.inference.executor import EXECUTE, InferenceTask
from gameshark.lifecycle import CancelToken

CONVERSATION = "stark:main"
RESPONSIBILITY = "chat:stark:main"
SYSTEM = ("You are Stark, a calm desktop companion. Answer in one or two short plain "
          "sentences.")
# Bytes of saved memory each reply may carry, apart from the history window.
MEMORY_BUDGET = 1024
SPEC = CallSpec(max_tokens=256, timeout_s=120.0, memory_budget=MEMORY_BUDGET)


QUEUED, THINKING, WAITING, FAILED = "queued", "thinking", "waiting", "failed"


def _suffix(retry: int) -> str:
    return f"-r{retry}" if retry else ""


def reply_event(seq: int, retry: int = 0) -> dict:
    payload = {"seq": seq, "retry": retry} if retry else {"seq": seq}
    return {"event_id": f"ev-reply-{CONVERSATION}-{seq}{_suffix(retry)}", "adapter": "stark",
            "type": "reply", "payload": payload}


def call_id_for(seq: int, retry: int = 0) -> str:
    return f"call:{CONVERSATION}:{seq}" + (f":r{retry}" if retry else "")


class StarkChatAdapter:
    name = "stark"
    required_caps: frozenset[str] = frozenset()

    def __init__(self, spec: CallSpec = SPEC, serve: bool = True,
                 privacy: Privacy = Privacy.LOCAL_ONLY) -> None:
        self.spec = spec
        self.serve = serve
        self.privacy = privacy
        self.ctx: AdapterContext | None = None
        self.pending: str | None = None      # call_id of the reply in flight, if any
        self._submitted: set[int] = set()    # seqs whose event this process submitted
        # (seq, error kind, wall clock retry time or None): why the head reply waits.
        self.waiting: tuple[int, str, float | None] | None = None

    # ------------------------------------------------------------------ lifecycle
    def init(self, ctx: AdapterContext) -> None:
        self.ctx = ctx
        journal = ctx.inference
        journal.open_conversation(CONVERSATION, self.privacy, responsibility_id=RESPONSIBILITY)
        if self.head() is None:
            journal.append(CONVERSATION, Role.SYSTEM, SYSTEM, 0)

    def reconnect(self, ctx: AdapterContext) -> None:
        self.ctx = ctx

    def teardown(self) -> None:
        self.ctx = None

    def build_effect(self, intent: dict):
        raise ValueError("the companion conversation emits no effects")

    # ------------------------------------------------------------------ reads
    def head(self) -> tuple[int, str] | None:
        """(seq, role) of the newest row, or None for an empty conversation."""
        assert self.ctx is not None
        row = self.ctx.conn.execute(
            "SELECT seq, role FROM messages WHERE conversation_id=? ORDER BY seq DESC LIMIT 1",
            (CONVERSATION,)).fetchone()
        return (row["seq"], row["role"]) if row else None

    def unanswered(self) -> int | None:
        h = self.head()
        return h[0] if h is not None and h[1] == Role.USER.value else None

    def _attempts(self, seq: int) -> list[tuple[int, str]]:
        """(retry, status) of every reply event for seq, oldest first."""
        assert self.ctx is not None
        base = reply_event(seq)["event_id"]
        # A key range, not LIKE: the pump reads this every tick, and only a range uses the
        # primary key index.
        rows = self.ctx.conn.execute(
            "SELECT event_id, status FROM events WHERE event_id=? OR "
            "(event_id>? AND event_id<?)", (base, f"{base}-r", f"{base}-s")).fetchall()
        out = []
        for r in rows:
            tail = r["event_id"][len(base):]
            if tail == "":
                out.append((0, r["status"]))
            elif tail[2:].isdigit():
                out.append((int(tail[2:]), r["status"]))
        return sorted(out)

    def reply_view(self, calls: list[tuple[str, str, str]]) -> dict | None:
        """What the head reply is doing, or None when no user row waits for one.

        queued: accepted, not yet streaming. thinking: the provider is streaming.
        waiting: a retryable refusal holds it (model down, timeout); error says which.
        failed: terminal and durable; a retry or a new message moves it on.
        """
        seq = self.unanswered()
        if seq is None:
            return None
        attempts = self._attempts(seq)
        retry = attempts[-1][0] if attempts else 0
        view: dict = {"seq": seq, "retry": retry}
        if attempts and attempts[-1][1] in ("acked", "dead"):
            assert self.ctx is not None
            r = self.ctx.responsibilities.get(RESPONSIBILITY)
            return {**view, "state": FAILED,
                    "error": (r.checkpoint if r is not None else None) or "unknown"}
        call = call_id_for(seq, retry)
        stage = next((st for cid, _conv, st in calls if cid == call), None)
        if stage == EXECUTE:
            return {**view, "state": THINKING}
        if self.waiting is not None and self.waiting[0] == seq:
            _, kind, at = self.waiting
            return {**view, "state": WAITING, "error": kind, "retry_at": at}
        return {**view, "state": QUEUED}

    # ------------------------------------------------------------------ controls
    def apply_control(self, action: str, payload: dict) -> str:
        """Operator controls. jack moves presence toward another surface (demo and tests);
        forget_memory forgets one saved memory for `stark.py memory forget`."""
        ctx = self.ctx
        assert ctx is not None
        if action == "forget_memory":
            if ctx.memory is None:
                raise ValueError("memory is not enabled")
            mid = str(payload.get("memory_id", ""))
            op = str(payload.get("op_id") or f"cli-{uuidlib.uuid4().hex}")
            try:
                m = ctx.memory.forget("operator-cli", op, mid)
            except LookupError as e:
                raise ValueError(str(e)) from None
            return f"{m.id}:{m.state}:history_mentions={m.history_mentions}"
        if action != "jack":
            raise ValueError(f"unknown action {action}")
        if ctx.presence is None:
            raise ValueError("presence is not enabled")
        to = str(payload.get("to", ""))
        if not to:
            raise ValueError("jack needs a target surface")
        h = ctx.presence.request_jack(payload.get("handoff_id") or f"jack-{uuidlib.uuid4()}",
                                      to)
        return f"{h.id}:{h.from_surface}->{h.to_surface}"

    # ------------------------------------------------------------------ replies
    def retry(self, seq: int) -> tuple[int, bool]:
        """Answer a failed reply again. Return (retry number, replayed).

        Only the newest retry of a failed head starts work; a resend, before or after its
        answer lands, returns that same retry. A seq with nothing failed is refused.
        """
        ctx = self.ctx
        assert ctx is not None and ctx.submit_event is not None
        attempts = self._attempts(seq)
        newest = attempts[-1] if attempts else None
        if newest is not None and newest[0] > 0 and (
                newest[1] == "pending" or self.unanswered() != seq):
            return newest[0], True
        if self.unanswered() != seq or newest is None or newest[1] not in ("acked", "dead"):
            raise ValueError(f"message {seq} has no failed reply to retry")
        n = newest[0] + 1
        self.waiting = None
        # The event is the one durable change: its handler marks the duty active, so a
        # crash before it persists leaves the reply failed and Retry still offered.
        ctx.submit_event(reply_event(seq, n))
        return n, False

    def ensure_reply(self) -> str | None:
        """Submit the reply event for an unanswered head row once per process."""
        ctx = self.ctx
        assert ctx is not None and ctx.submit_event is not None
        seq = self.unanswered()
        if seq is None or seq in self._submitted:
            return None
        self._submitted.add(seq)
        return ctx.submit_event(reply_event(seq))

    def poll(self, ctx: AdapterContext) -> dict:
        self.ctx = ctx
        self.ensure_reply()
        return {}

    def on_event(self, event: dict):
        ctx = self.ctx
        assert ctx is not None
        seq = int(event["payload"]["seq"])
        retry = int(event["payload"].get("retry", 0))
        if self.unanswered() != seq:
            # Answered, or superseded by a newer user row whose own event replies.
            self.pending = None
            self._settle()
            return None
        if not self.serve:
            return Deferred(math.inf)        # the serving daemon replays it
        # Created on the first message, so a Stark nobody talked to owns no chat duty.
        ctx.responsibilities.upsert(RESPONSIBILITY, kind="conversation",
                                    payload={"conversation_id": CONVERSATION}, state="active")
        ctx.responsibilities.transition(RESPONSIBILITY, "active")
        call_id = call_id_for(seq, retry)
        self.pending = call_id
        return InferenceTask(call_id, CONVERSATION, self.spec,
                             on_done=lambda r: self._answered(call_id, r),
                             on_error=lambda e: self._failed(call_id, e),
                             on_retry=lambda e, at: self._waiting(seq, e, at))

    def _waiting(self, seq: int, exc: Exception, at: float) -> None:
        # Wall clock, fixed once per refusal, so the pushed view changes only with it.
        when = None if math.isinf(at) else round(time.time() + at - time.monotonic(), 1)
        self.waiting = (seq, type(exc).__name__, when)

    def _answered(self, call_id: str, result: RouteResult) -> None:
        if self.pending == call_id:
            self.pending = None
        self.waiting = None
        self._settle()

    def _failed(self, call_id: str, exc: Exception) -> None:
        if self.pending == call_id:
            self.pending = None
        self.waiting = None
        assert self.ctx is not None
        self.ctx.responsibilities.transition(RESPONSIBILITY, "blocked",
                                             checkpoint=type(exc).__name__)

    def _settle(self) -> None:
        assert self.ctx is not None
        if self.ctx.responsibilities.get(RESPONSIBILITY) is None:
            return
        if self.unanswered() is None and self.pending is None:
            self.ctx.responsibilities.transition(RESPONSIBILITY, "done")


# ---------------------------------------------------------------------- demo provider
class CompanionFakeProvider(FakeProvider):
    """Deterministic local provider for the demo: a readable reply streamed word by word.

    delay_s spreads over the words, so the surface shows Stark working for that long while
    the coordinator keeps answering. Output is a pure function of the request.
    """

    def __init__(self, delay_s: float = 2.0, name: str = "companion-fake",
                 sleep: Callable[[float, CancelToken], None] | None = None) -> None:
        kwargs = {"sleep": sleep} if sleep is not None else {}
        super().__init__(name, Capabilities("companion-fake", Locality.LOCAL, 32768,
                                            max_concurrency=2), **kwargs)
        self.delay_s = delay_s

    def default_text(self, req: ProviderRequest) -> tuple[str, ...]:
        last = next((m.content for m in reversed(req.messages) if m.role is Role.USER), "")
        users = sum(1 for m in req.messages if m.role is Role.USER)
        words = f"[fake model] Got it. You said: {_clip(last)} (message {users})".split()
        return tuple(w + " " for w in words[:-1]) + (words[-1],)

    def generate(self, req: ProviderRequest, cancel: CancelToken) -> FakeCall:
        n = len(self.default_text(req))
        self._scripts.setdefault(req.call_id, Script(delays=tuple([self.delay_s / n] * n)))
        return super().generate(req, cancel)


def _clip(text: str, words: int = 12) -> str:
    parts = text.split()
    return " ".join(parts[:words]) + (" ..." if len(parts) > words else "")
