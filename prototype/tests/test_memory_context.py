"""Memory injection: a separate bounded budget, deterministic selection, a privacy filter,
and a commit fence so no reply built on a forgotten or corrected memory lands."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.authz import GrantStore
from gameshark.inference import (
    CallSpec,
    Capabilities,
    ContextConflict,
    FakeProvider,
    InferenceJournal,
    Locality,
    Privacy,
    ProviderConfig,
    Role,
)
from gameshark.inference.journal import HISTORY_PER_MESSAGE
from gameshark.inference.router import CLOUD_GRANT, GRANT_ADAPTER, InferenceRouter
from gameshark.lease import LeaseManager
from gameshark.lifecycle import CancelToken
from gameshark.memory import GRANT_ADAPTER as MEMORY_GRANTS
from gameshark.memory import HEADER, INJECT, MemoryStore
from gameshark.presence import PresenceManager
from gameshark.store import connect
from tests.helpers import AGENT, HOST

CONV = "stark:main"
SYSTEM = "You are Stark."


class Env:
    def __init__(self, db: Path, clock) -> None:
        self.conn = connect(db)
        self.clock = clock
        self.lease = LeaseManager(self.conn, AGENT, clock)
        self.gen = self.lease.acquire(HOST, 30.0)
        self.pm = PresenceManager(self.conn, AGENT, self.lease, self.gen, clock)
        self.pm.ensure_home("mac")
        self.grants = GrantStore(self.conn, AGENT, clock)
        self.memory = MemoryStore(self.conn, AGENT, self.lease, self.gen, self.pm,
                                  self.grants, clock)
        self.memory.ensure_grants()
        self.journal = InferenceJournal(self.conn, AGENT, self.lease, self.gen, clock,
                                        memory=self.memory)
        self.seq = 0
        self.ops = 0

    def conversation(self, privacy: Privacy = Privacy.LOCAL_ONLY) -> None:
        self.journal.open_conversation(CONV, privacy)
        self.add(Role.SYSTEM, SYSTEM)

    def add(self, role: Role, text: str) -> None:
        self.journal.append(CONV, role, text, self.seq)
        self.seq += 1

    def remember(self, text: str, cloud_ok: bool = False):
        self.ops += 1
        return self.memory.remember("mac", 1, f"op{self.ops}", text,
                                    privacy=Privacy.CLOUD_OK if cloud_ok else Privacy.LOCAL_ONLY)

    def forget(self, mid: str):
        self.ops += 1
        return self.memory.forget("mac", f"op{self.ops}", mid)

    def router(self, *providers: FakeProvider) -> InferenceRouter:
        return InferenceRouter(self.journal, [ProviderConfig(p) for p in providers],
                               grants=self.grants)


@pytest.fixture
def env(paths, clock) -> Env:
    return Env(paths["runtime"], clock)


SPEC = CallSpec(max_tokens=16, memory_budget=512)


def memory_block(req) -> str | None:
    hits = [m.content for m in req.messages if m.role is Role.SYSTEM and
            m.content.startswith(HEADER)]
    assert len(hits) <= 1
    return hits[0] if hits else None


def test_saved_fact_reaches_the_model_after_its_turn_left_the_window(env):
    env.conversation()
    env.add(Role.USER, "my locker number is 1188")
    env.remember("Locker number is 1188.")
    for i in range(15):
        env.add(Role.USER, f"filler turn {i} " + "y" * 80)
    p = FakeProvider("local")
    spec = CallSpec(max_tokens=16, history_budget=400, memory_budget=256)
    env.router(p).complete("c1", CONV, spec, CancelToken())
    req = p.invocations[-1]
    history = [m.content for m in req.messages if not m.content.startswith(HEADER)]
    assert not any("1188" in h for h in history)       # the original turn is gone
    assert "Locker number is 1188." in memory_block(req)
    assert req.messages[0].content == SYSTEM           # system first, memory right after
    assert memory_block(req) == req.messages[1].content


def test_no_memories_means_no_block(env):
    env.conversation()
    env.add(Role.USER, "hi")
    p = FakeProvider("local")
    env.router(p).complete("c1", CONV, SPEC, CancelToken())
    assert memory_block(p.invocations[-1]) is None
    row = env.conn.execute("SELECT request FROM inference_calls").fetchone()
    assert "memories" not in json.loads(row["request"])


def test_spec_without_a_memory_budget_injects_nothing(env):
    env.conversation()
    env.add(Role.USER, "hi")
    env.remember("a fact")
    p = FakeProvider("local")
    env.router(p).complete("c1", CONV, CallSpec(max_tokens=16), CancelToken())
    assert memory_block(p.invocations[-1]) is None


def test_block_respects_its_own_budget(env):
    env.conversation()
    env.add(Role.USER, "tell me things")
    for i in range(40):
        env.remember(f"fact number {i} " + "w" * 30)
    p = FakeProvider("local")
    budget = 300
    env.router(p).complete("c1", CONV, CallSpec(max_tokens=16, memory_budget=budget),
                           CancelToken())
    block = memory_block(p.invocations[-1])
    assert block is not None
    assert len(block.encode()) + HISTORY_PER_MESSAGE <= budget
    assert 0 < block.count("\n- ") < 40


def test_relevant_memories_win_the_budget(env):
    env.conversation()
    for i in range(20):
        env.remember(f"unrelated detail {i} " + "v" * 30)
    env.remember("Favorite climbing gym is Stone Summit.")
    env.add(Role.USER, "which climbing gym do I like?")
    p = FakeProvider("local")
    env.router(p).complete("c1", CONV, CallSpec(max_tokens=16, memory_budget=200),
                           CancelToken())
    assert "Stone Summit" in memory_block(p.invocations[-1])


def test_selection_is_deterministic_across_replays(env):
    env.conversation()
    env.add(Role.USER, "what do you know")
    for t in ("c", "a", "b"):
        env.remember(f"fact {t}")
    first = env.journal.request_messages(CONV, env.seq, SPEC, Locality.LOCAL)
    again = env.journal.request_messages(CONV, env.seq, SPEC, Locality.LOCAL)
    assert first == again
    block = memory_block(type("R", (), {"messages": first[0]}))
    assert block.index("fact c") < block.index("fact a") < block.index("fact b")


def test_forgotten_memory_never_returns_through_retrieval(env):
    env.conversation()
    env.add(Role.USER, "what is my code")
    m = env.remember("Gate code is 9314.")
    env.forget(m.id)
    p = FakeProvider("local")
    env.router(p).complete("c1", CONV, SPEC, CancelToken())
    assert memory_block(p.invocations[-1]) is None
    assert all("9314" not in x.content for x in p.invocations[-1].messages)


def test_corrected_memory_injects_only_the_new_text(env):
    env.conversation()
    env.add(Role.USER, "where do I live")
    m = env.remember("Lives in Boston.")
    env.memory.correct("mac", 1, "fix", m.id, 1, "Lives in Atlanta.")
    p = FakeProvider("local")
    env.router(p).complete("c1", CONV, SPEC, CancelToken())
    block = memory_block(p.invocations[-1])
    assert "Atlanta" in block and "Boston" not in block


def test_inject_revocation_stops_injection_without_deleting(env):
    env.conversation()
    env.add(Role.USER, "hello")
    env.remember("a fact")
    env.grants.revoke(MEMORY_GRANTS, INJECT)
    p = FakeProvider("local")
    env.router(p).complete("c1", CONV, SPEC, CancelToken())
    assert memory_block(p.invocations[-1]) is None
    assert len(env.memory.list()) == 1


def test_forget_between_reserve_and_commit_discards_the_reply(env):
    env.conversation()
    env.add(Role.USER, "what is my code")
    m = env.remember("Gate code is 9314.")
    p = FakeProvider("local")
    res = env.journal.reserve("c1", CONV, p.name, p.capabilities(), SPEC)
    assert res.memories == ((m.id, 1),)
    stored = json.loads(env.conn.execute("SELECT request FROM inference_calls").fetchone()[0])
    assert stored["memories"] == [[m.id, 1]]                 # ids only, never content
    result = env.journal.collect(res, p.generate(res.request, CancelToken()))
    env.forget(m.id)
    with pytest.raises(ContextConflict):
        env.journal.commit(res, result)
    assert env.conn.execute("SELECT COUNT(*) FROM messages WHERE role='assistant'"
                            ).fetchone()[0] == 0
    # The owner replays the same call; the new attempt carries no forgotten fact.
    res2 = env.journal.reserve("c1", CONV, p.name, p.capabilities(), SPEC)
    assert res2.memories == () and memory_block(res2.request) is None
    env.journal.commit(res2, env.journal.collect(res2, p.generate(res2.request,
                                                                  CancelToken())))


def test_correction_between_reserve_and_commit_discards_the_reply(env):
    env.conversation()
    env.add(Role.USER, "where do I live")
    m = env.remember("Lives in Boston.")
    p = FakeProvider("local")
    res = env.journal.reserve("c1", CONV, p.name, p.capabilities(), SPEC)
    result = env.journal.collect(res, p.generate(res.request, CancelToken()))
    env.memory.correct("mac", 1, "fix", m.id, 1, "Lives in Atlanta.")
    with pytest.raises(ContextConflict):
        env.journal.commit(res, result)


def test_a_new_memory_during_a_call_does_not_block_its_commit(env):
    env.conversation()
    env.add(Role.USER, "hello")
    env.remember("first fact")
    p = FakeProvider("local")
    res = env.journal.reserve("c1", CONV, p.name, p.capabilities(), SPEC)
    result = env.journal.collect(res, p.generate(res.request, CancelToken()))
    env.remember("second fact")
    assert env.journal.commit(res, result).content


def test_cloud_provider_never_sees_local_only_memories(env):
    env.conversation(Privacy.CLOUD_OK)
    env.add(Role.USER, "what do you know")
    env.remember("private local fact")
    env.remember("shareable fact", cloud_ok=True)
    env.grants.ensure_granted(GRANT_ADAPTER, frozenset({CLOUD_GRANT}))
    cloud = FakeProvider("cloud", Capabilities("cloud-model", Locality.CLOUD, 4096))
    env.router(cloud).complete("c1", CONV, SPEC, CancelToken())
    block = memory_block(cloud.invocations[-1])
    assert "shareable fact" in block and "private local fact" not in block


def test_memory_survives_a_model_switch(paths, clock):
    a = Env(paths["runtime"], clock)
    a.conversation()
    a.add(Role.USER, "remember my dog")
    a.remember("Dog is named Biscuit.")
    first = FakeProvider("model-a")
    a.router(first).complete("c1", CONV, SPEC, CancelToken())
    a.conn.close()
    b = Env(paths["runtime"], clock)                      # restart onto another model
    b.seq = 3
    b.add(Role.USER, "what is my dog called")
    second = FakeProvider("model-b", Capabilities("model-b", Locality.LOCAL, 8192))
    b.router(second).complete("c2", CONV, SPEC, CancelToken())
    for p in (first, second):
        assert "Biscuit" in memory_block(p.invocations[-1])


def test_memory_counts_toward_the_window_check(env):
    env.conversation()
    env.add(Role.USER, "hi")
    env.remember("x" * 600)
    tiny = FakeProvider("tiny", Capabilities("tiny", Locality.LOCAL, 120))
    from gameshark.inference.router import BoundKind, TokenBound

    router = InferenceRouter(env.journal, [ProviderConfig(
        tiny, token_bound=TokenBound(BoundKind.TRUSTED_BYTES))], grants=env.grants)
    from gameshark.inference import ContextTooLong

    with pytest.raises(ContextTooLong):
        router.complete("c1", CONV, CallSpec(max_tokens=16, memory_budget=1024),
                        CancelToken())
