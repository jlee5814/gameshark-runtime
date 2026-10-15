"""Step 6 primitives: async inference owned by events, run on a bounded executor.

The test thread is the coordinator: it owns the supervisor's connection and pumps
completions, as the daemon's tick thread does. Providers block on explicit gates, never on
sleeps, so every interleaving below is forced rather than hoped for.
"""

from __future__ import annotations

import threading
import time

import pytest

from gameshark.errors import CrashInjected, FencedError
from gameshark.inference import (
    Capabilities,
    Fault,
    FinishReason,
    Locality,
    ProviderConfig,
    RouterPolicy,
    Script,
    Timeout,
    TrackerPolicy,
)
from gameshark.inference.fake import FakeProvider
from gameshark.lease import LeaseManager
from gameshark.store import connect
from gameshark.supervisor import AgentSupervisor
from sims.game import GameSimulator
from tests.helpers import AGENT, HOST
from tests.inference_adapter import AsyncAssistantAdapter, ask_event, call_id_for, reply_key
from tests.test_inference_journal import (
    CALL,
    CRASH_POINTS,
    EXPECT,
    answers,
    assert_recovered,
    ledger,
    reserved_count,
)
from tests.test_model_tracker import Deaf

TTL = 30.0


# ---------------------------------------------------------------------- helpers
class Gate:
    """A provider sleep that blocks until opened. Cooperative: a cancel ends the wait."""

    def __init__(self) -> None:
        self.open = threading.Event()
        self._lock = threading.Lock()
        self.waiting = 0

    def sleep(self, _seconds: float, token) -> None:
        with self._lock:
            self.waiting += 1
        try:
            while not self.open.wait(0.005):
                if token.cancelled:
                    return
        finally:
            with self._lock:
                self.waiting -= 1


def call_for(topic: str) -> str:
    return call_id_for(ask_event(topic)["event_id"])


def fake(name: str = "local", slots: int = 2, scripts=None, **kw) -> FakeProvider:
    return FakeProvider(name, Capabilities(f"{name}-model", Locality.LOCAL, 4096,
                                           max_concurrency=slots), scripts=scripts, **kw)


def gated(gate: Gate, *topics: str, slots: int = 2, **script) -> FakeProvider:
    scripts = {call_for(t): Script(delays=(1.0,), **script) for t in topics}
    return fake(slots=slots, scripts=scripts, sleep=gate.sleep)


def boot(root, clock, *providers, owner: str = HOST, crash_at=None, ttl: float = TTL,
         tracker_policy: TrackerPolicy | None = None, **inference) -> AgentSupervisor:
    sup = AgentSupervisor(db_path=root / "runtime.db", agent_id=AGENT, owner_token=owner,
                          clock=clock, crash_at=crash_at, lease_ttl_s=ttl)
    sup.add_adapter(AsyncAssistantAdapter(GameSimulator(root / "game.db")))
    inference.setdefault("retry_s", 0.0)
    sup.enable_inference([ProviderConfig(p) for p in providers],
                         policy=RouterPolicy(agent_in_flight=16),
                         tracker_policy=tracker_policy, **inference)
    sup.start()
    return sup


def crash(sup: AgentSupervisor) -> None:
    """Model process death: drop the instance without releasing anything durable."""
    sup.executor.close()
    sup.tracker.close()
    sup.close()


def pump_until(sup: AgentSupervisor, pred, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        sup.pump_inference()
        sup.redeliver()
        if pred():
            return
        sup.executor.wake.wait(0.01)
    raise AssertionError("condition not reached")


def event_status(sup, topic: str = "t1") -> str:
    return sup.router.status(ask_event(topic)["event_id"])


def game(root) -> GameSimulator:
    return GameSimulator(root / "game.db")


def assert_clean(sup: AgentSupervisor) -> None:
    """No leaked call, slot, queue entry, reservation, claim, or tracker use."""
    ex = sup.executor.stats()
    assert (ex.tasks, ex.jobs_queued, ex.completions_queued) == (0, 0, 0)
    assert ex.peak_completions <= sup.executor.depth
    tr = sup.tracker
    assert all(p.in_flight == 0 and p.queued == 0 for p in tr.scheduler.stats().values())
    assert tr.scheduler.calls_in_flight() == ({}, {})
    for cfg, _ in tr.configs():
        assert tr._active[cfg.name].in_flight == 0 and not tr._active[cfg.name].uses
    assert reserved_count(sup.conn) == 0
    assert sup.router.claimed() == frozenset()
    assert sup.inference._active == {}


# ---------------------------------------------------------------------- event ownership
def test_event_stays_pending_during_inference_then_acks_once(tmp_path, clock):
    gate = Gate()
    provider = gated(gate, "t1")
    sup = boot(tmp_path, clock, provider)
    assert sup.submit(ask_event()) == "deferred"
    pump_until(sup, lambda: gate.waiting == 1)

    # Running: pending, claimed, reserved, nothing committed or posted.
    assert event_status(sup) == "pending"
    assert sup.router.claimed() == {ask_event()["event_id"]}
    assert ledger(sup.conn) == [(1, "reserved")] and answers(sup.conn) == []
    assert sup.responsibility_state("ask:t1") == "active"
    # Redelivery and replay of a running event never dispatch it twice.
    assert sup.submit(ask_event()) == "claimed"
    assert sup.router.replay() == ["claimed"]
    assert provider.count() == 1

    gate.open.set()
    pump_until(sup, lambda: event_status(sup) == "acked")
    assert len(answers(sup.conn)) == 1 and ledger(sup.conn) == [(1, "settled")]
    assert sup.effect_status(reply_key(CALL)) == "done"
    assert game(tmp_path).move_count(reply_key(CALL)) == 1
    assert sup.responsibility_state("ask:t1") == "done"
    assert sup.conn.execute("SELECT delivered_count FROM events").fetchone()[0] == 1
    assert provider.count() == 1
    assert_clean(sup)
    sup.stop()
    sup.close()


def test_only_the_coordinator_thread_touches_the_connection(tmp_path, clock):
    gate = Gate()
    sup = boot(tmp_path, clock, gated(gate, "t1", "t2"))
    threads: set[int] = set()
    sup.conn.set_trace_callback(lambda _sql: threads.add(threading.get_ident()))
    sup.submit(ask_event("t1"))
    sup.submit(ask_event("t2"))
    pump_until(sup, lambda: gate.waiting == 2)
    gate.open.set()
    pump_until(sup, lambda: event_status(sup, "t1") == event_status(sup, "t2") == "acked")
    sup.conn.set_trace_callback(None)
    assert threads == {threading.get_ident()}
    assert sup.executor.stats().threads_alive == 2
    assert_clean(sup)
    sup.stop()
    assert sup.executor.stats().threads_alive == 0
    sup.close()


def test_retryable_failure_releases_the_event_and_redelivery_reuses_the_call_id(tmp_path, clock):
    first = fake(scripts={(CALL, 1): Script(fault=Fault(1, lambda: Timeout("slow")))})
    sup = boot(tmp_path, clock, first)
    assert sup.submit(ask_event()) == "deferred"
    pump_until(sup, lambda: first.count() >= 2)  # Timeout is retryable: same call_id again
    pump_until(sup, lambda: event_status(sup) == "acked")
    assert [r.attempt for r in first.invocations] == [1, 2]
    assert {r.call_id for r in first.invocations} == {CALL}
    assert ledger(sup.conn) == [(1, "lost"), (2, "settled")]
    assert len(answers(sup.conn)) == 1
    assert_clean(sup)
    sup.stop()
    sup.close()


def test_retries_exhaust_to_a_blocked_responsibility_and_one_ack(tmp_path, clock):
    always = Script(fault=Fault(0, lambda: Timeout("slow")))
    provider = fake(scripts={(CALL, a): always for a in (1, 2, 3)})
    sup = boot(tmp_path, clock, provider)
    sup.submit(ask_event())
    pump_until(sup, lambda: event_status(sup) == "acked")
    assert provider.count() == 3
    assert sup.responsibility_state("ask:t1") == "blocked"
    assert ledger(sup.conn) == [(1, "lost"), (2, "lost"), (3, "lost")]
    assert answers(sup.conn) == [] and sup.effect_status(reply_key(CALL)) is None
    assert_clean(sup)
    sup.stop()
    sup.close()


def test_draining_owner_starts_no_inference_and_keeps_the_event(tmp_path, clock):
    provider = fake()
    sup = boot(tmp_path, clock, provider)
    sup.lifecycle.begin_drain(time.monotonic() + 10)
    sup.router.deliver(ask_event())  # an in flight replay, past the submit gate
    assert event_status(sup) == "pending" and provider.count() == 0
    assert sup.redeliver() == []
    assert ledger(sup.conn) == []
    assert_clean(sup)
    sup.stop()
    sup.close()


# ---------------------------------------------------------------------- bounds
def test_concurrent_calls_stay_within_provider_slots(tmp_path, clock):
    gate = Gate()
    provider = gated(gate, "t1", "t2", "t3", slots=2)
    sup = boot(tmp_path, clock, provider, completion_depth=8)
    outcomes = [sup.submit(ask_event(t)) for t in ("t1", "t2", "t3")]
    assert outcomes == ["deferred"] * 3
    pump_until(sup, lambda: gate.waiting == 2)
    # The third found no slot: refused before reserve, pending, unclaimed, not dispatched.
    assert event_status(sup, "t3") == "pending"
    assert ask_event("t3")["event_id"] not in sup.router.claimed()
    assert provider.count() == 2 and ledger(sup.conn, call_for("t3")) == []

    gate.open.set()
    pump_until(sup, lambda: all(event_status(sup, t) == "acked" for t in ("t1", "t2", "t3")))
    assert provider.peak_in_flight == 2
    assert sup.tracker.scheduler.stats()["local"].peak_in_flight == 2
    assert all(len(answers(sup.conn, call_for(t))) == 1 for t in ("t1", "t2", "t3"))
    assert_clean(sup)
    sup.stop()
    sup.close()


def test_completion_depth_bounds_calls_in_flight(tmp_path, clock):
    gate = Gate()
    provider = gated(gate, "t1", "t2", slots=4)
    sup = boot(tmp_path, clock, provider, completion_depth=1)
    assert sup.executor.threads == 4 and sup.executor.depth == 1
    sup.submit(ask_event("t1"))
    sup.submit(ask_event("t2"))  # refused: the executor holds its bound already
    pump_until(sup, lambda: gate.waiting == 1)
    assert sup.executor.stats().refused == 1 and sup.executor.in_flight == 1
    assert provider.count() == 1

    gate.open.set()
    pump_until(sup, lambda: event_status(sup, "t1") == event_status(sup, "t2") == "acked")
    assert sup.executor.stats().peak_completions == 1
    assert provider.peak_in_flight == 1
    assert_clean(sup)
    sup.stop()
    sup.close()


# ---------------------------------------------------------------------- ownership loss
@pytest.mark.parametrize("loss", ["expired", "takeover"])
def test_late_completion_after_lease_loss_commits_and_acks_nothing(tmp_path, clock, loss):
    gate = Gate()
    sup = boot(tmp_path, clock, gated(gate, "t1"), ttl=5.0)
    sup.submit(ask_event())
    pump_until(sup, lambda: gate.waiting == 1)
    clock.advance(6.0)
    if loss == "takeover":
        conn = connect(tmp_path / "runtime.db")
        assert LeaseManager(conn, AGENT, clock).acquire("successor", 5.0) == sup.generation + 1
        conn.close()
    gate.open.set()
    with pytest.raises(FencedError):
        pump_until(sup, lambda: False)

    assert answers(sup.conn) == []
    assert event_status(sup) == "pending"
    assert sup.effect_status(reply_key(CALL)) is None
    assert game(tmp_path).move_count(reply_key(CALL)) == 0
    assert ledger(sup.conn) == [(1, "reserved")]  # the successor's recovery settles it
    sup.abandon_inference(write=False)
    ex = sup.executor.stats()
    assert ex.tasks == 0 and sup.router.claimed() == frozenset()
    assert sup.tracker.scheduler.calls_in_flight() == ({}, {})
    crash(sup)


# ---------------------------------------------------------------------- replacement
def test_replacement_evicts_an_unsealed_completion_and_the_successor_answers(tmp_path,
                                                                             clock):
    gate = threading.Event()
    old = Deaf(fake(scripts={CALL: Script(chunks=("stale",), delays=(1.0,))},
                    sleep=lambda _s, _t: gate.wait(5)))
    new = fake()
    sup = boot(tmp_path, clock, old, tracker_policy=TrackerPolicy(drain_grace_s=0.05))
    sup.submit(ask_event())
    pump_until(sup, lambda: old.inner.in_flight == 1)
    retired = sup.tracker._active["local"]
    report = sup.tracker.replace(ProviderConfig(new), drain_timeout_s=0.05)
    assert report.cancelled == 1 and old.tokens[0].cancelled  # evicted, still running
    gate.set()  # the deaf model answers anyway, for the retired configuration
    pump_until(sup, lambda: event_status(sup) == "acked")

    assert [c for _, _, c in answers(sup.conn)] != ["stale"]
    assert new.count(CALL) == 1 and old.inner.count(CALL) == 1
    assert ledger(sup.conn) == [(1, "lost"), (2, "settled")]
    assert sup.effect_status(reply_key(CALL)) == "done"
    assert retired.in_flight == 0 and not retired.uses
    assert_clean(sup)
    sup.stop()
    sup.close()


def test_sealed_completion_drains_on_the_old_configuration(tmp_path, clock):
    gate = Gate()
    old, new = gated(gate, "t1"), fake()
    sup = boot(tmp_path, clock, old)
    sup.submit(ask_event())
    pump_until(sup, lambda: gate.waiting == 1)
    gate.open.set()
    while sup.executor.stats().completions_queued == 0:  # answered, not yet committed
        sup.executor.wake.wait(0.01)
    reports = []
    rt = threading.Thread(target=lambda: reports.append(
        sup.tracker.replace(ProviderConfig(new), drain_timeout_s=5.0)))
    rt.start()
    pump_until(sup, lambda: event_status(sup) == "acked")  # seal, commit, release
    rt.join(10)
    assert reports[0].cancelled == 0 and reports[0].drained == 1
    assert old.count(CALL) == 1 and new.count(CALL) == 0
    assert ledger(sup.conn) == [(1, "settled")]
    assert sup.conn.execute("SELECT instance_ref FROM inference_calls").fetchone()[0] \
        .startswith("local:1.")
    assert_clean(sup)
    sup.stop()
    sup.close()


# ---------------------------------------------------------------------- output safety
@pytest.mark.parametrize(("script", "reason"), [
    (Script(chunks=("half ", "an"), finish_reason=FinishReason.LENGTH), "IncompleteAnswer"),
    (Script(chunks=("<think>", "move rook", "</think>")), "UnvalidatedReasoning"),
    (Script(chunks=("<think>", "still thinking")), "UnvalidatedReasoning"),
    (Script(chunks=()), "EmptyAnswer"),
    (Script(chunks=("call ",), finish_reason=FinishReason.TOOL_CALLS),
     "UnsupportedFinish:tool_calls"),
])
def test_unusable_answers_commit_as_data_but_cause_no_effect(tmp_path, clock, script, reason):
    sup = boot(tmp_path, clock, fake(scripts={CALL: script}))
    sup.submit(ask_event())
    pump_until(sup, lambda: event_status(sup) == "acked")
    assert len(answers(sup.conn)) == 1  # the model's text is kept, as data
    assert sup.responsibility_state("ask:t1") == "blocked"
    assert sup.responsibilities.get("ask:t1").checkpoint == reason
    assert sup.effect_status(reply_key(CALL)) is None
    assert game(tmp_path).move_count(reply_key(CALL)) == 0
    sup.stop()
    sup.close()
    # A replay sees the same finish reason: the judgment survives restart.
    again = boot(tmp_path, clock, fake())
    assert again.inference.lookup(CALL).finish_reason is script.finish_reason
    again.stop()
    again.close()


# ---------------------------------------------------------------------- crash points
@pytest.mark.parametrize("label", CRASH_POINTS)
def test_crash_point_in_the_async_path_recovers_to_one_answer(tmp_path, clock, label):
    old = fake()
    sup = boot(tmp_path, clock, old, crash_at={label})
    uuid = sup.identity.uuid
    sup.submit(ask_event())
    with pytest.raises(CrashInjected):
        pump_until(sup, lambda: False)
    crash(sup)

    new = fake()
    sup = boot(tmp_path, clock, new)  # recovery settles, replay reissues the same call_id
    pump_until(sup, lambda: event_status(sup) == "acked")
    old_calls, new_calls, expected_ledger = EXPECT[label]
    assert old.count() == old_calls
    assert {a: new.count(CALL, a) for a in (1, 2, 3) if new.count(CALL, a)} == new_calls
    assert ledger(sup.conn) == expected_ledger
    assert_recovered(sup, uuid)
    assert_clean(sup)
    sup.stop()
    sup.close()
