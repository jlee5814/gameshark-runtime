"""Inference journal: one committed answer per call_id across crashes, owners, and races.

A restart discards the supervisor and its FakeProvider and builds fresh ones on the same
files, so a provider counter that survives a restart proves nothing. Every case runs on a
FakeClock; threads order themselves with barriers, never sleeps.
"""

from __future__ import annotations

import random
import signal
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.clock import FakeClock
from gameshark.effects import Effect
from gameshark.errors import CrashInjected, FencedError, UncommittedOutput
from gameshark.inference import (
    AttemptsExhausted,
    CallCancelled,
    CallSpec,
    Capabilities,
    Chunk,
    Committed,
    ContextConflict,
    ContextTooLong,
    FakeProvider,
    Fault,
    FinishReason,
    InferenceJournal,
    InvalidRequest,
    Locality,
    Message,
    OwnershipMismatch,
    Privacy,
    ProviderCancelled,
    ProviderResult,
    ResultMismatch,
    Role,
    Script,
    StaleAttempt,
    StreamInterrupted,
    Usage,
)
from gameshark.inference.types import MAX_ATTEMPTS
from gameshark.lease import LeaseManager
from gameshark.lifecycle import CancelToken
from gameshark.store import connect
from gameshark.supervisor import AgentSupervisor
from sims.game import GameSimulator
from tests.helpers import AGENT, HOST
from tests.inference_adapter import (
    SPEC,
    SYSTEM,
    AssistantAdapter,
    ask_event,
    call_id_for,
    conversation_for,
    reply_key,
)
from tests.test_lease_fencing import snapshot

ROOT = Path(__file__).resolve().parents[1]
CRASH_POINTS = ["E1_before_reserve", "E2_after_reserve", "E3_after_provider", "E4_after_commit"]
CALL = call_id_for(ask_event()["event_id"])
CONV = conversation_for("t1")
TTL = 10.0


# ---------------------------------------------------------------------- helpers
def boot(root: Path, clock, provider: FakeProvider | None = None, owner: str = HOST,
         crash_at: set[str] | None = None, ttl: float = 30.0,
         start: bool = True) -> AgentSupervisor:
    sup = AgentSupervisor(db_path=root / "runtime.db", agent_id=AGENT, owner_token=owner,
                          clock=clock, crash_at=crash_at, lease_ttl_s=ttl)
    sup.add_adapter(AssistantAdapter(provider or FakeProvider(), GameSimulator(root / "game.db")))
    if start:
        sup.start()
    return sup


def assistant(sup) -> AssistantAdapter:
    return sup.registry.get("assistant")


def provider_of(sup) -> FakeProvider:
    return assistant(sup).provider


def rows(conn, sql: str, *params) -> list[tuple]:
    return [tuple(r) for r in conn.execute(sql, params).fetchall()]


def answers(conn, call_id: str = CALL) -> list[tuple]:
    return rows(conn, "SELECT conversation_id, seq, content FROM messages WHERE call_id=?",
                call_id)


def ledger(conn, call_id: str = CALL) -> list[tuple]:
    return rows(conn, "SELECT attempt, status FROM budget_ledger WHERE call_id=? "
                "ORDER BY attempt", call_id)


def call_row(conn, call_id: str = CALL) -> sqlite3.Row:
    return conn.execute("SELECT * FROM inference_calls WHERE call_id=?", (call_id,)).fetchone()


def reserved_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM budget_ledger WHERE status='reserved'").fetchone()[0]


def seqs(conn, cid: str = CONV) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT seq FROM messages WHERE conversation_id=? ORDER BY seq", (cid,))]


def seed_conversation(journal: InferenceJournal, cid: str = CONV,
                      question: str = ask_event()["payload"]["question"],
                      privacy: Privacy = Privacy.LOCAL_ONLY) -> None:
    journal.open_conversation(cid, privacy)
    journal.append(cid, Role.SYSTEM, SYSTEM, 0)
    journal.append(cid, Role.USER, question, 1)


def run_attempt(journal: InferenceJournal, provider: FakeProvider, call_id: str = CALL,
                cid: str = CONV, spec: CallSpec = SPEC):
    res = journal.reserve(call_id, cid, provider.name, provider.capabilities(), spec)
    call = provider.generate(res.request, CancelToken())
    return res, journal.collect(res, call)


def assert_recovered(sup, expected_uuid: str) -> None:
    """The acceptance shape for every crash point."""
    assert sup.identity.uuid == expected_uuid
    assert len(answers(sup.conn)) == 1
    assert seqs(sup.conn) == [0, 1, 2]
    assert reserved_count(sup.conn) == 0
    assert call_row(sup.conn)["status"] == "done"
    assert sup.effect_status(reply_key(CALL)) == "done"
    assert GameSimulator(sup.db_path.replace("runtime.db", "game.db")).move_count(
        reply_key(CALL)) == 1
    assert sup.responsibility_state("ask:t1") == "done"


# Expected (old process invocations, new process invocations by attempt, ledger) per point.
EXPECT = {
    "E1_before_reserve": (0, {1: 1}, [(1, "settled")]),
    "E2_after_reserve": (0, {2: 1}, [(1, "lost"), (2, "settled")]),
    "E3_after_provider": (1, {2: 1}, [(1, "lost"), (2, "settled")]),
    "E4_after_commit": (1, {}, [(1, "settled")]),
}


# ---------------------------------------------------------------------- happy path
def test_ask_commits_one_message_settles_ledger_and_posts_once(tmp_path, clock):
    sup = boot(tmp_path, clock)
    assert sup.submit(ask_event()) == "acked"

    p = provider_of(sup)
    req = p.invocations[0]
    expected = "".join(p.default_text(req))
    assert answers(sup.conn) == [(CONV, 2, expected)]
    assert [m.role for m in req.messages] == [Role.SYSTEM, Role.USER]
    row = call_row(sup.conn)
    assert (row["status"], row["attempts"], row["provider"], row["generation"]) == (
        "done", 1, "fake", sup.generation)
    led = sup.conn.execute("SELECT * FROM budget_ledger").fetchone()
    assert (led["status"], led["tokens_out"]) == ("settled", 8)
    assert sup.effect_status(reply_key(CALL)) == "done"
    assert assistant(sup).outcomes == ["committed"]
    sup.close()


# ---------------------------------------------------------------------- crash points
@pytest.mark.parametrize("label", CRASH_POINTS)
def test_crash_point_recovers_to_one_answer_50_seeds(tmp_path, label):
    old_count, new_counts, expected_ledger = EXPECT[label]
    for seed in range(50):
        rng = random.Random(seed)
        root = tmp_path / str(seed)
        root.mkdir()
        clock = FakeClock()
        question = " ".join(f"q{rng.randrange(1000)}" for _ in range(rng.randint(1, 12)))
        event = ask_event(question=question)

        first = boot(root, clock, crash_at={label})
        uuid = first.identity.uuid
        with pytest.raises(CrashInjected, match=label):
            first.submit(event)
        assert provider_of(first).count() == old_count
        first.close()

        second = boot(root, clock, provider=FakeProvider())   # start() replays the event
        p = provider_of(second)
        assert {a: p.count(CALL, a) for a in (1, 2, 3) if p.count(CALL, a)} == new_counts
        assert ledger(second.conn) == expected_ledger
        lost = second.conn.execute(
            "SELECT reserved_micros, cost_micros FROM budget_ledger WHERE status='lost'"
        ).fetchall()
        assert all(r[0] == r[1] for r in lost)                # charged at full reservation
        assert_recovered(second, uuid)
        second.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("label", CRASH_POINTS)
def test_sigkill_at_crash_point_recovers_to_one_answer(tmp_path, clock, label):
    _, new_counts, expected_ledger = EXPECT[label]
    uuid_out = tmp_path / "uuid.txt"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "inference_worker.py"), str(tmp_path / "runtime.db"),
         str(tmp_path / "game.db"), str(uuid_out), label],
        capture_output=True, text=True, timeout=30, check=False, cwd=ROOT)
    assert proc.returncode == -signal.SIGKILL, proc.stdout + proc.stderr

    sup = boot(tmp_path, clock, provider=FakeProvider())
    p = provider_of(sup)
    assert {a: p.count(CALL, a) for a in (1, 2, 3) if p.count(CALL, a)} == new_counts
    assert ledger(sup.conn) == expected_ledger
    assert_recovered(sup, uuid_out.read_text(encoding="utf8"))
    sup.close()


def test_replay_of_done_call_short_circuits_without_provider(tmp_path, clock):
    sup = boot(tmp_path, clock)
    sup.submit(ask_event())
    first = answers(sup.conn)
    sup.close()

    again = boot(tmp_path, clock, provider=FakeProvider())
    p = provider_of(again)
    got = again.inference.complete(CALL, CONV, p, SPEC, CancelToken())
    assert got.replayed and (got.conversation_id, got.seq, got.content) == first[0]
    assert again.inference.lookup(CALL) == got
    assert p.count() == 0
    assert again.submit(ask_event()) == "duplicate"
    assert answers(again.conn) == first
    again.close()


# ---------------------------------------------------------------------- duplicates and races
def test_duplicate_commit_of_one_reservation_writes_one_row(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed_conversation(sup.inference)
    res, result = run_attempt(sup.inference, FakeProvider())
    first = sup.inference.commit(res, result)
    second = sup.inference.commit(res, result)
    assert not first.replayed and second.replayed
    assert (second.seq, second.content) == (first.seq, first.content)
    assert len(answers(sup.conn)) == 1 and seqs(sup.conn) == [0, 1, 2]
    sup.close()


def _in_thread(db: Path, clock, generation: int, barrier: threading.Barrier, fn, out: dict,
               key) -> threading.Thread:
    """Run fn(journal) on its own connection, released together with its peers."""

    def body() -> None:
        conn = connect(db)
        journal = InferenceJournal(conn, AGENT, LeaseManager(conn, AGENT, clock), generation,
                                   clock)
        barrier.wait()
        try:
            out[key] = fn(journal)
        except Exception as exc:
            out[key] = exc
        finally:
            conn.close()

    t = threading.Thread(target=body)
    t.start()
    return t


def test_concurrent_commits_of_two_attempts_keep_only_the_latest(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed_conversation(sup.inference)
    p = FakeProvider(scripts={(CALL, 1): Script(chunks=("one",)),
                              (CALL, 2): Script(chunks=("two",))})
    res1, out1 = run_attempt(sup.inference, p)
    res2, out2 = run_attempt(sup.inference, p)     # a duplicate owner reserved attempt 2
    barrier, out = threading.Barrier(2), {}
    threads = [
        _in_thread(sup.db_path, clock, sup.generation, barrier,
                   lambda j, r=r, o=o: j.commit(r, o), out, r.attempt)
        for r, o in ((res1, out1), (res2, out2))
    ]
    for t in threads:
        t.join(timeout=10)
    # Whichever lands second, the stale attempt never writes: it is refused, or it finds
    # the call done and gets the committed answer back.
    assert isinstance(out[1], StaleAttempt) or (out[1].replayed and out[1].content == "two")
    assert isinstance(out[2], Committed) and out[2].content == "two"
    assert answers(sup.conn) == [(CONV, 2, "two")]
    assert ledger(sup.conn) == [(1, "settled"), (2, "settled")]
    sup.close()


def test_concurrent_commit_of_one_reservation_from_two_connections(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed_conversation(sup.inference)
    res, result = run_attempt(sup.inference, FakeProvider())
    barrier, out = threading.Barrier(2), {}
    threads = [_in_thread(sup.db_path, clock, sup.generation, barrier,
                          lambda j: j.commit(res, result), out, k) for k in "ab"]
    for t in threads:
        t.join(timeout=10)
    assert sorted(out[k].replayed for k in "ab") == [False, True]
    assert len(answers(sup.conn)) == 1
    sup.close()


def test_concurrent_calls_on_distinct_conversations_all_commit(tmp_path, clock):
    sup = boot(tmp_path, clock)
    n = 8
    for i in range(n):
        seed_conversation(sup.inference, cid=f"c{i}", question=f"question {i}")
    p = FakeProvider()
    barrier, out = threading.Barrier(n), {}
    threads = [
        _in_thread(sup.db_path, clock, sup.generation, barrier,
                   lambda j, i=i: j.complete(f"k{i}", f"c{i}", p, SPEC, CancelToken()), out, i)
        for i in range(n)
    ]
    for t in threads:
        t.join(timeout=20)
    assert all(isinstance(out[i], Committed) and out[i].seq == 2 for i in range(n)), out
    for i in range(n):
        assert seqs(sup.conn, f"c{i}") == [0, 1, 2]
        assert answers(sup.conn, f"k{i}")[0][0] == f"c{i}"   # each answer in its own conversation
    assert p.count() == n
    sup.close()


def test_two_calls_on_one_conversation_conflict_then_rebase(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed_conversation(sup.inference)
    p = FakeProvider()
    res_a, out_a = run_attempt(sup.inference, p, call_id="a")
    res_b, out_b = run_attempt(sup.inference, p, call_id="b")
    assert res_a.base_seq == res_b.base_seq == 2
    sup.inference.commit(res_a, out_a)
    with pytest.raises(ContextConflict):
        sup.inference.commit(res_b, out_b)
    assert answers(sup.conn, "b") == []
    assert ledger(sup.conn, "b") == [(1, "settled")]            # the attempt was still spent
    assert call_row(sup.conn, "b")["status"] == "pending"

    got = sup.inference.complete("b", CONV, p, SPEC, CancelToken())
    assert got.seq == 3 and seqs(sup.conn) == [0, 1, 2, 3]
    last = p.invocations[-1]
    assert (last.attempt, len(last.messages), last.messages[-1].content) == (2, 3, out_a.text)
    sup.close()


# ---------------------------------------------------------------------- result identity
class ForgedCall:
    """A ProviderCall that misreports itself, to prove the journal checks every piece."""

    def __init__(self, request, chunks, result):
        self.request = request
        self._chunks = chunks
        self._result = result

    def __iter__(self):
        return iter(self._chunks)

    def result(self):
        return self._result

    def close(self):
        pass


def _result(call_id, attempt, text):
    return ProviderResult(call_id, attempt, text, FinishReason.STOP, Usage(1, 1), "m")


def test_foreign_chunks_and_results_are_rejected_and_write_nothing(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed_conversation(sup.inference)
    j, p = sup.inference, FakeProvider()
    res = j.reserve(CALL, CONV, p.name, p.capabilities(), SPEC)
    other = j.reserve("other", CONV, p.name, p.capabilities(), SPEC)
    before = snapshot(sup.conn)

    forged = {
        "foreign stream": p.generate(other.request, CancelToken()),
        "foreign chunk": ForgedCall(res.request, [Chunk("other", 0, "x")], _result(CALL, 1, "x")),
        "index gap": ForgedCall(res.request, [Chunk(CALL, 1, "x")], _result(CALL, 1, "x")),
        "foreign result": ForgedCall(res.request, [Chunk(CALL, 0, "x")], _result("other", 1, "x")),
        "wrong attempt": ForgedCall(res.request, [Chunk(CALL, 0, "x")], _result(CALL, 2, "x")),
        "text drift": ForgedCall(res.request, [Chunk(CALL, 0, "x")], _result(CALL, 1, "xy")),
    }
    for name, call in forged.items():
        with pytest.raises(ResultMismatch):
            j.collect(res, call)
        assert snapshot(sup.conn) == before, name
    with pytest.raises(ResultMismatch):
        j.commit(res, _result("other", 1, "x"))
    assert snapshot(sup.conn) == before

    # Interleaved real streams on one provider stay separate.
    a, b = p.generate(res.request, CancelToken()), p.generate(other.request, CancelToken())
    ia, ib = iter(a), iter(b)
    for _ in range(8):
        assert next(ia).call_id == CALL and next(ib).call_id == "other"
    for it in (ia, ib):
        assert next(it, None) is None
    assert a.result().call_id == CALL and b.result().call_id == "other"
    sup.close()


# ---------------------------------------------------------------------- fencing
def _owner_with_answer(tmp_path, clock, owner="host-1"):
    sup = boot(tmp_path, clock, owner=owner, ttl=TTL)
    seed_conversation(sup.inference)
    res, result = run_attempt(sup.inference, FakeProvider())
    return sup, res, result


def test_stale_owner_cannot_commit_after_takeover(tmp_path, clock):
    old, res, result = _owner_with_answer(tmp_path, clock)
    clock.advance(TTL + 1)
    new = boot(tmp_path, clock, owner="host-2", ttl=TTL)
    assert new.generation == old.generation + 1
    assert ledger(new.conn) == [(1, "lost")]                 # recovery settled the orphan
    before = snapshot(new.conn)
    with pytest.raises(FencedError, match="stale generation"):
        old.inference.commit(res, result)
    assert snapshot(new.conn) == before

    got = new.inference.complete(CALL, CONV, FakeProvider(), SPEC, CancelToken())
    assert answers(new.conn) == [(CONV, 2, got.content)]
    assert call_row(new.conn)["generation"] == new.generation
    assert ledger(new.conn) == [(1, "lost"), (2, "settled")]
    old.close()
    new.close()


def test_released_owner_cannot_commit(tmp_path, clock):
    sup, res, result = _owner_with_answer(tmp_path, clock)
    sup.stop()
    before = snapshot(sup.conn)
    with pytest.raises(FencedError, match="released"):
        sup.inference.commit(res, result)
    assert snapshot(sup.conn) == before
    sup.close()


def test_expired_owner_with_no_successor_cannot_commit(tmp_path, clock):
    sup, res, result = _owner_with_answer(tmp_path, clock)
    clock.advance(TTL)                                        # now == expires_at
    before = snapshot(sup.conn)
    for attempt in (lambda: sup.inference.commit(res, result),
                    lambda: sup.inference.reserve("x", CONV, "fake",
                                                  FakeProvider().capabilities(), SPEC),
                    lambda: sup.inference.cancel(CALL)):
        with pytest.raises(FencedError, match="expired"):
            attempt()
        assert not sup.conn.in_transaction
    assert snapshot(sup.conn) == before
    assert sup.lease.current_generation() == sup.generation  # nobody took over
    sup.close()


def test_same_generation_zombie_cannot_commit_a_settled_attempt(tmp_path, clock):
    # A same owner restart on a live lease keeps the generation, so the guard alone cannot
    # stop the old incarnation. Recovery settled its reservation lost; the commit refuses.
    old, res, result = _owner_with_answer(tmp_path, clock, owner=HOST)
    new = boot(tmp_path, clock, owner=HOST, ttl=TTL)
    assert new.generation == old.generation
    with pytest.raises(StaleAttempt):
        old.inference.commit(res, result)
    assert answers(new.conn) == []
    old.close()
    new.close()


# ---------------------------------------------------------------------- sequence
def test_append_is_idempotent_and_refuses_conflicts(tmp_path, clock):
    sup = boot(tmp_path, clock)
    j = sup.inference
    seed_conversation(j)
    seed_conversation(j)                                      # replay: same rows, no change
    assert seqs(sup.conn) == [0, 1]
    with pytest.raises(ContextConflict):
        j.append(CONV, Role.USER, "different", 1)
    with pytest.raises(ContextConflict):
        j.append(CONV, Role.USER, "gap", 5)
    with pytest.raises(InvalidRequest):
        j.append(CONV, Role.ASSISTANT, "forged answer", 2)
    assert seqs(sup.conn) == [0, 1]
    sup.close()


def test_call_and_conversation_ownership_are_enforced(tmp_path, clock):
    sup = boot(tmp_path, clock)
    j, p = sup.inference, FakeProvider()
    seed_conversation(j)
    seed_conversation(j, cid="other")
    j.complete(CALL, CONV, p, SPEC, CancelToken())
    with pytest.raises(OwnershipMismatch):
        j.reserve(CALL, "other", p.name, p.capabilities(), SPEC)
    sup.conn.execute(
        "INSERT INTO conversations VALUES('foreign','someone',NULL,'local_only',0,0,0)")
    with pytest.raises(OwnershipMismatch):
        j.append("foreign", Role.USER, "hi", 0)
    with pytest.raises(OwnershipMismatch):
        j.open_conversation("foreign")
    assert len(answers(sup.conn)) == 1
    sup.close()


# ---------------------------------------------------------------------- retry limit
def test_attempt_cap_survives_crash_loop_restarts(tmp_path, clock):
    invocations = 0
    for run in range(MAX_ATTEMPTS):
        sup = boot(tmp_path, clock, crash_at={"E3_after_provider"}, start=False)
        with pytest.raises(CrashInjected):
            if run == 0:
                sup.start()
                sup.submit(ask_event())
            else:
                sup.start()                                   # replay crashes again
        invocations += provider_of(sup).count()
        sup.close()
    assert invocations == MAX_ATTEMPTS

    final = boot(tmp_path, clock, provider=FakeProvider())
    assert provider_of(final).count() == 0
    row = call_row(final.conn)
    assert (row["status"], row["attempts"], row["error_kind"]) == (
        "failed", 3, "AttemptsExhausted")
    assert ledger(final.conn) == [(1, "lost"), (2, "lost"), (3, "lost")]
    assert answers(final.conn) == []
    assert final.responsibility_state("ask:t1") == "blocked"
    assert final.router.status(ask_event()["event_id"]) == "acked"
    assert final.effect_status(reply_key(CALL)) is None
    with pytest.raises(AttemptsExhausted):
        final.inference.lookup(CALL)
    final.close()


# ---------------------------------------------------------------------- cancellation
def test_drain_cancel_mid_stream_keeps_partial_text_out_and_call_pending(tmp_path, clock):
    def cancel_on_sleep(_s, token):
        token.cancel("drain deadline")                         # the deadline lands mid stream

    p = FakeProvider(scripts={CALL: Script(delays=(0, 0, 1.0))}, sleep=cancel_on_sleep)
    sup = boot(tmp_path, clock, provider=p)
    with pytest.raises(ProviderCancelled):
        sup.submit(ask_event())
    assert answers(sup.conn) == []
    row = call_row(sup.conn)
    assert (row["status"], row["attempts"]) == ("pending", 1)
    assert ledger(sup.conn) == [(1, "lost")]
    assert sup.router.status(ask_event()["event_id"]) == "pending"
    sup.close()

    successor = boot(tmp_path, clock, provider=FakeProvider())
    assert provider_of(successor).count(CALL, 2) == 1
    assert len(answers(successor.conn)) == 1
    successor.close()


def test_operator_cancel_discards_a_late_result_and_never_reruns(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed_conversation(sup.inference)
    res, result = run_attempt(sup.inference, FakeProvider())
    assert sup.inference.cancel(CALL) is True
    with pytest.raises(CallCancelled):
        sup.inference.commit(res, result)
    assert answers(sup.conn) == [] and seqs(sup.conn) == [0, 1]
    assert ledger(sup.conn) == [(1, "settled")]               # spent, never committed
    with pytest.raises(CallCancelled):
        sup.inference.lookup(CALL)
    sup.close()

    again = boot(tmp_path, clock, provider=FakeProvider())
    assert again.submit(ask_event()) == "acked"
    assert provider_of(again).count() == 0
    assert again.responsibility_state("ask:t1") == "blocked"
    assert again.inference.cancel(CALL) is False              # terminal stays terminal
    again.close()


def test_interrupted_stream_commits_nothing(tmp_path, clock):
    p = FakeProvider(scripts={CALL: Script(fault=Fault(3, StreamInterrupted))})
    sup = boot(tmp_path, clock, provider=p)
    with pytest.raises(StreamInterrupted):
        sup.submit(ask_event())
    assert answers(sup.conn) == []
    assert ledger(sup.conn) == [(1, "lost")]
    assert call_row(sup.conn)["status"] == "pending"
    sup.close()


def test_pre_dispatch_refusal_consumes_no_attempt_or_budget(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed_conversation(sup.inference)
    p = FakeProvider()
    with pytest.raises(ContextTooLong):
        sup.inference.complete(CALL, CONV, p, CallSpec(max_tokens=5000), CancelToken())
    assert call_row(sup.conn) is None              # refused in reserve: nothing written
    assert ledger(sup.conn) == [] and p.count() == 0
    sup.inference.complete(CALL, CONV, p, SPEC, CancelToken())
    assert ledger(sup.conn) == [(1, "settled")]
    sup.close()


# ---------------------------------------------------------------------- budget
def test_reservation_is_durable_before_dispatch_and_lost_charges_in_full(tmp_path, clock):
    caps = Capabilities(model="cloud-m", locality=Locality.CLOUD, context_window=4096,
                        price_in_micros=1000, price_out_micros=2000)
    seen = {}

    def observe(_s, _token):
        # A separate connection sees the reservation while the stream is still open.
        conn = sqlite3.connect(tmp_path / "runtime.db")
        seen["ledger"] = conn.execute("SELECT attempt, status FROM budget_ledger").fetchall()
        conn.close()

    p = FakeProvider("cloud", capabilities=caps, sleep=observe,
                     scripts={CALL: Script(delays=(0.1,))})
    sup = boot(tmp_path, clock)
    seed_conversation(sup.inference, question="four words of prompt")
    sup.inference.set_budget(3600.0, max_tokens=10_000, max_cost_micros=1_000_000)
    got = sup.inference.complete(CALL, CONV, p, SPEC, CancelToken())
    assert seen["ledger"] == [(1, "reserved")]
    led = sup.conn.execute("SELECT * FROM budget_ledger").fetchone()
    prompt = len(SYSTEM.split()) + 4
    assert led["reserved_tokens"] == prompt + SPEC.max_tokens
    assert led["reserved_micros"] == prompt * 1 + SPEC.max_tokens * 2
    assert led["cost_micros"] == prompt * 1 + len(got.content.split()) * 2
    assert sup.conn.execute("SELECT * FROM agent_budgets").fetchone()["max_tokens"] == 10_000

    # An abandoned reservation charges its full reserve.
    sup.inference.append(CONV, Role.USER, "and then", 3)
    res = sup.inference.reserve("k2", CONV, p.name, caps, SPEC)
    sup.inference._active.clear()                             # model the process dying
    assert sup.inference.settle_abandoned() == [("k2", 1)]
    lost = sup.conn.execute("SELECT * FROM budget_ledger WHERE call_id='k2'").fetchone()
    assert (lost["status"], lost["cost_micros"]) == ("lost", res.reserved_micros)
    assert sup.inference.spend()["micros"] == led["cost_micros"] + res.reserved_micros
    sup.close()


# ---------------------------------------------------------------------- identity and effects
def test_provider_swap_keeps_identity_and_resends_full_context(tmp_path, clock):
    sup = boot(tmp_path, clock)
    uuid = sup.identity.uuid
    seed_conversation(sup.inference)
    first = sup.inference.complete("k1", CONV, FakeProvider("local-a"), SPEC, CancelToken())
    sup.close()

    again = boot(tmp_path, clock)
    again.inference.append(CONV, Role.USER, "follow up", 3)
    b = FakeProvider("local-b")
    again.inference.complete("k2", CONV, b, SPEC, CancelToken())
    sent = b.invocations[0].messages
    assert [m.content for m in sent] == [SYSTEM, "what is the plan", first.content,
                                         "follow up"]
    assert again.identity.uuid == uuid
    assert rows(again.conn, "SELECT agent_id FROM conversations") == [(AGENT,)]
    assert rows(again.conn, "SELECT call_id, provider FROM inference_calls ORDER BY call_id") == [
        ("k1", "local-a"), ("k2", "local-b")]
    again.close()


def test_effect_citing_an_uncommitted_call_is_refused(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed_conversation(sup.inference)
    sup.inference.reserve(CALL, CONV, "fake", FakeProvider().capabilities(), SPEC)
    adapter = assistant(sup)
    eff: Effect = adapter.build_effect({"call_id": CALL, "conversation_id": CONV,
                                        "text": "unreviewed model text",
                                        "responsibility_id": "ask:t1"})
    with pytest.raises(UncommittedOutput):
        adapter.ctx.emit_effect(eff)
    assert sup.effect_status(reply_key(CALL)) is None
    sup.close()


# ---------------------------------------------------------------------- history window
def _m(role: Role, text: str) -> Message:
    return Message(role, text)


def test_fit_history_keeps_leading_system_rows_and_the_newest_suffix():
    from gameshark.inference.journal import HISTORY_FIXED, HISTORY_PER_MESSAGE, fit_history

    msgs = (_m(Role.SYSTEM, "s" * 20),) + tuple(
        _m(Role.USER if i % 2 == 0 else Role.ASSISTANT, f"{i}" * 30) for i in range(10))
    assert fit_history(msgs, None) == msgs
    out = fit_history(msgs, 200)
    assert out[0] == msgs[0] and out[-1] == msgs[-1]
    assert out[1:] == msgs[len(msgs) - (len(out) - 1):]          # a contiguous suffix
    assert out[1].role is Role.USER                               # starting on a user turn
    assert sum(len(m.content.encode()) + HISTORY_PER_MESSAGE for m in out) + HISTORY_FIXED <= 200
    bigger = fit_history(msgs, 300)
    assert len(bigger) > len(out)
    # The newest row always rides along, even past the budget: the router then refuses it
    # as ContextTooLong rather than sending a request without the question.
    assert fit_history(msgs, 1) == (msgs[0], msgs[-1])


def test_fit_history_is_deterministic_for_reserve_and_select(tmp_path, clock):
    sup = boot(tmp_path, clock)
    j = sup.inference
    seed_conversation(j)
    for i in range(6):
        j.append(CONV, Role.USER, "u" * 50, j.conversation(CONV)["next_seq"])
    nxt = j.conversation(CONV)["next_seq"]
    assert j.context(CONV, nxt, 150) == j.context(CONV, nxt, 150)
    assert len(j.context(CONV, nxt, 150)) < len(j.context(CONV, nxt))
    sup.close()


def test_reserve_refuses_a_rebuilt_context_the_window_cannot_hold(tmp_path, clock):
    """Codex P2: select checked an earlier context; a user row that lands before reserve
    rebuilds a larger one, and fit_history keeps the newest row even past the budget.
    Reserve checks the rebuilt prompt against this candidate's window itself."""
    from gameshark.inference.types import prompt_bytes_bound

    sup = boot(tmp_path, clock)
    j = sup.inference
    seed_conversation(j)
    j.append(CONV, Role.USER, "x" * 8000, j.conversation(CONV)["next_seq"])
    caps = Capabilities(model="small", locality=Locality.LOCAL, context_window=1024)
    spec = CallSpec(max_tokens=512, history_budget=256)
    with pytest.raises(ContextTooLong):
        j.reserve(CALL, CONV, "small", caps, spec,
                  prompt_tokens=lambda m: prompt_bytes_bound(m, 8, 16))
    assert sup.conn.execute("SELECT COUNT(*) FROM inference_calls").fetchone()[0] == 0
    assert ledger(sup.conn) == []
    sup.close()
