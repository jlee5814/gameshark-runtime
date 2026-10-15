"""Test adapter whose event handler owns one inference call per event.

An ask event opens a conversation, appends its system and user messages at fixed seqs,
completes one call whose call_id derives from the event id, and posts the committed reply
to the game simulator through the effect journal. The effect intent cites the call_id, so
the supervisor refuses it unless the call committed (R5). Replay of the event is the only
path that reissues the call; recovery never drives an inference row on its own.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.adapters import AdapterContext
from gameshark.effects import Effect
from gameshark.inference import (
    AttemptsExhausted,
    CallCancelled,
    CallSpec,
    FakeProvider,
    FinishReason,
    Privacy,
    Role,
    RouteResult,
)
from gameshark.inference.executor import InferenceTask
from sims.game import GameSimulator

SYSTEM = "you are stark and you answer briefly"
SPEC = CallSpec(max_tokens=16)


def call_id_for(event_id: str) -> str:
    return "call:" + hashlib.sha256(event_id.encode()).hexdigest()[:16]


def ask_event(topic: str = "t1", question: str = "what is the plan") -> dict:
    return {
        "event_id": f"ev-ask-{topic}",
        "adapter": "assistant",
        "type": "ask",
        "payload": {"topic": topic, "question": question},
    }


def conversation_for(topic: str) -> str:
    return f"conv:{topic}"


def reply_key(call_id: str) -> str:
    return f"reply:{call_id}"


class AssistantAdapter:
    name = "assistant"
    required_caps = frozenset({"assistant.reply"})

    def __init__(self, provider: FakeProvider | None, remote: GameSimulator,
                 spec: CallSpec = SPEC) -> None:
        self.provider = provider
        self.remote = remote
        self.spec = spec
        self.ctx: AdapterContext | None = None
        self.outcomes: list[str] = []

    def init(self, ctx: AdapterContext) -> None:
        self.ctx = ctx

    def reconnect(self, ctx: AdapterContext) -> None:
        self.ctx = ctx

    def teardown(self) -> None:
        self.ctx = None

    def _effect(self, call_id: str, conversation_id: str, text: str, rid: str) -> Effect:
        key = reply_key(call_id)
        intent = {"adapter": self.name, "call_id": call_id, "conversation_id": conversation_id,
                  "text": text, "responsibility_id": rid}
        return Effect(
            key=key, intent=intent, required_cap="assistant.reply", adapter=self.name,
            do=lambda fence: self.remote.submit_move(key, conversation_id, 0, text, fence),
            probe=lambda: self.remote.probe(key),
        )

    def build_effect(self, intent: dict) -> Effect:
        return self._effect(intent["call_id"], intent["conversation_id"], intent["text"],
                            intent["responsibility_id"])

    def on_event(self, event: dict) -> None:
        ctx = self.ctx
        assert ctx is not None
        topic = event["payload"]["topic"]
        cid, rid, call_id = conversation_for(topic), f"ask:{topic}", call_id_for(event["event_id"])
        ctx.responsibilities.upsert(rid, kind="assistant", payload={"topic": topic},
                                    state="active")
        journal = ctx.inference
        journal.open_conversation(cid, Privacy.LOCAL_ONLY, responsibility_id=rid)
        journal.append(cid, Role.SYSTEM, SYSTEM, 0)
        journal.append(cid, Role.USER, event["payload"]["question"], 1)
        try:
            committed = journal.complete(call_id, cid, self.provider, self.spec, ctx.cancel)
        except (AttemptsExhausted, CallCancelled) as exc:
            ctx.responsibilities.transition(rid, "blocked", checkpoint=type(exc).__name__)
            self.outcomes.append(type(exc).__name__)
            return
        self.outcomes.append("replayed" if committed.replayed else "committed")
        ctx.emit_effect(self._effect(call_id, cid, committed.content, rid))
        ctx.responsibilities.transition(rid, "done")


# ---------------------------------------------------------------------- async (step 6)
REASONING_MARKERS = ("<think>", "</think>")


def unusable(committed) -> str | None:
    """Why a committed answer must not drive an effect, or None when it may.

    Model text is data. Only a complete, plain answer is posted as a reply, and never
    parsed into a command. Reasoning extraction is model specific and out of scope, so any
    reasoning marker makes the text unvalidated rather than something to strip.
    """
    if committed.finish_reason is FinishReason.LENGTH:
        return "IncompleteAnswer"
    if committed.finish_reason is not FinishReason.STOP:
        return f"UnsupportedFinish:{committed.finish_reason and committed.finish_reason.value}"
    if any(m in committed.content for m in REASONING_MARKERS):
        return "UnvalidatedReasoning"
    if not committed.content.strip():
        return "EmptyAnswer"
    return None


class AsyncAssistantAdapter(AssistantAdapter):
    """The same ask flow, but the handler returns an InferenceTask instead of blocking.

    The supervisor runs the call on its executor and keeps the event pending until the
    continuation below ran; providers come from enable_inference, not from the adapter.
    An ask control turns into an ask event, so operators reach it through control_requests.
    """

    def __init__(self, remote: GameSimulator, spec: CallSpec = SPEC) -> None:
        super().__init__(None, remote, spec)

    def apply_control(self, action: str, payload: dict) -> str:
        if action != "ask":
            raise ValueError(f"unknown action {action}")
        assert self.ctx is not None and self.ctx.submit_event is not None
        return self.ctx.submit_event(ask_event(payload["topic"], payload["question"]))

    def on_event(self, event: dict) -> InferenceTask:
        ctx = self.ctx
        assert ctx is not None
        topic = event["payload"]["topic"]
        cid, rid, call_id = conversation_for(topic), f"ask:{topic}", call_id_for(event["event_id"])
        ctx.responsibilities.upsert(rid, kind="assistant", payload={"topic": topic},
                                    state="active")
        journal = ctx.inference
        journal.open_conversation(cid, Privacy.LOCAL_ONLY, responsibility_id=rid)
        journal.append(cid, Role.SYSTEM, SYSTEM, 0)
        journal.append(cid, Role.USER, event["payload"]["question"], 1)
        return InferenceTask(call_id, cid, self.spec,
                             on_done=lambda r: self._answered(call_id, cid, rid, r),
                             on_error=lambda e: self._failed(rid, e))

    def _answered(self, call_id: str, cid: str, rid: str, result: RouteResult) -> None:
        ctx = self.ctx
        assert ctx is not None
        committed = result.committed
        reason = unusable(committed)
        if reason is not None:
            ctx.responsibilities.transition(rid, "blocked", checkpoint=reason)
            self.outcomes.append(reason)
            return
        self.outcomes.append("replayed" if committed.replayed else "committed")
        ctx.emit_effect(self._effect(call_id, cid, committed.content, rid))
        ctx.responsibilities.transition(rid, "done")

    def _failed(self, rid: str, exc: Exception) -> None:
        assert self.ctx is not None
        self.ctx.responsibilities.transition(rid, "blocked", checkpoint=type(exc).__name__)
        self.outcomes.append(type(exc).__name__)
