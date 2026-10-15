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
"""

from __future__ import annotations

import math
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
from gameshark.inference.executor import InferenceTask
from gameshark.lifecycle import CancelToken

CONVERSATION = "stark:main"
RESPONSIBILITY = "chat:stark:main"
SYSTEM = ("You are Stark, a calm desktop companion. Answer in one or two short plain "
          "sentences.")
SPEC = CallSpec(max_tokens=256, timeout_s=120.0)


def reply_event(seq: int) -> dict:
    return {"event_id": f"ev-reply-{CONVERSATION}-{seq}", "adapter": "stark",
            "type": "reply", "payload": {"seq": seq}}


def call_id_for(seq: int) -> str:
    return f"call:{CONVERSATION}:{seq}"


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

    # ------------------------------------------------------------------ controls
    def apply_control(self, action: str, payload: dict) -> str:
        """Operator controls. jack moves presence toward another surface (demo and tests)."""
        ctx = self.ctx
        assert ctx is not None
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
        call_id = call_id_for(seq)
        self.pending = call_id
        return InferenceTask(call_id, CONVERSATION, self.spec,
                             on_done=lambda r: self._answered(call_id, r),
                             on_error=lambda e: self._failed(call_id, e))

    def _answered(self, call_id: str, result: RouteResult) -> None:
        if self.pending == call_id:
            self.pending = None
        self._settle()

    def _failed(self, call_id: str, exc: Exception) -> None:
        if self.pending == call_id:
            self.pending = None
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
        words = f"Got it. You said: {_clip(last)} (message {users})".split()
        return tuple(w + " " for w in words[:-1]) + (words[-1],)

    def generate(self, req: ProviderRequest, cancel: CancelToken) -> FakeCall:
        n = len(self.default_text(req))
        self._scripts.setdefault(req.call_id, Script(delays=tuple([self.delay_s / n] * n)))
        return super().generate(req, cancel)


def _clip(text: str, words: int = 12) -> str:
    parts = text.split()
    return " ".join(parts[:words]) + (" ..." if len(parts) > words else "")
