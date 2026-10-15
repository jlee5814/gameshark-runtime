"""Inference router: selection, policy, admission, budgets, slots, failover, journal reuse.

Every case runs on FakeProvider and a FakeClock. Threads that race on SQLite each open
their own connection on the same file and share one in memory Scheduler, the shape the
worker executor will take in increment 6. A restart discards the supervisor, its router,
its scheduler, and its providers.
"""

from __future__ import annotations

import math
import random
import sqlite3
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.authz import GrantStore
from gameshark.clock import FakeClock
from gameshark.inference import (
    CLOUD_GRANT,
    GRANT_ADAPTER,
    SOFT_BUDGET_GRANT,
    AttemptsExhausted,
    AuthFailed,
    BoundKind,
    BudgetExceeded,
    BudgetUnenforceable,
    CallCancelled,
    CallSpec,
    Capabilities,
    Capability,
    CapabilityUnsupported,
    ContextTooLong,
    EgressDenied,
    FakeProvider,
    Fault,
    Health,
    HealthStatus,
    InferenceJournal,
    InferenceRouter,
    InvalidRequest,
    Locality,
    ModelCrashed,
    Overloaded,
    Privacy,
    ProviderCancelled,
    ProviderConfig,
    ProviderError,
    ProviderHealth,
    ProviderUnavailable,
    RateLimited,
    Role,
    RouterPolicy,
    Scheduler,
    Script,
    StreamInterrupted,
    Timeout,
    TokenBound,
    Usage,
)
from gameshark.inference.budget import window_start
from gameshark.inference.types import MAX_ATTEMPTS, prompt_bytes_bound
from gameshark.lease import LeaseManager
from gameshark.lifecycle import CancelToken
from gameshark.store import connect
from gameshark.supervisor import AgentSupervisor
from sims.game import GameSimulator
from tests.helpers import AGENT, HOST
from tests.inference_adapter import SYSTEM, AssistantAdapter, ask_event, call_id_for

QUESTION = "what is the plan"
SPEC = CallSpec(max_tokens=16)
TRUSTED = TokenBound(BoundKind.TRUSTED_BYTES)
HOUR = 3600.0


# ---------------------------------------------------------------------- helpers
def local(name: str = "local", slots: int = 2, window: int = 4096, features=()) -> FakeProvider:
    return FakeProvider(name, Capabilities(f"{name}-model", Locality.LOCAL, window,
                                           frozenset(features), max_concurrency=slots))


def cloud(name: str = "cloud", slots: int = 4, price_in: int = 1000, price_out: int = 2000,
          features=(), **kw) -> FakeProvider:
    return FakeProvider(name, Capabilities(f"{name}-model", Locality.CLOUD, 8192,
                                           frozenset(features), max_concurrency=slots,
                                           price_in_micros=price_in, price_out_micros=price_out),
                        **kw)


def cfg(provider, bound: TokenBound = TRUSTED, **kw) -> ProviderConfig:
    return ProviderConfig(provider, token_bound=bound, **kw)


def boot(root: Path, clock, ttl: float = 30.0) -> AgentSupervisor:
    sup = AgentSupervisor(db_path=root / "runtime.db", agent_id=AGENT, owner_token=HOST,
                          clock=clock, lease_ttl_s=ttl)
    sup.start()
    return sup


def seed(journal: InferenceJournal, cid: str = "c", privacy: Privacy = Privacy.LOCAL_ONLY,
         question: str = QUESTION) -> str:
    journal.open_conversation(cid, privacy)
    journal.append(cid, Role.SYSTEM, SYSTEM, 0)
    journal.append(cid, Role.USER, question, 1)
    return cid


def grant(sup, *caps: str) -> None:
    sup.grants.ensure_granted(GRANT_ADAPTER, frozenset(caps))


def router(sup, *configs, policy: RouterPolicy | None = None, **kw) -> InferenceRouter:
    return InferenceRouter(sup.inference, configs, policy=policy, **kw)


def ledger(conn, call_id: str) -> list[tuple]:
    return [tuple(r) for r in conn.execute(
        "SELECT attempt, provider, status FROM budget_ledger WHERE call_id=? ORDER BY attempt",
        (call_id,))]


def call_row(conn, call_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM inference_calls WHERE call_id=?", (call_id,)).fetchone()


def answers(conn, call_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM messages WHERE call_id=?", (call_id,)).fetchone()[0]


def reserved_rows(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM budget_ledger WHERE status='reserved'").fetchone()[0]


def bytes_reservation(provider: FakeProvider, max_tokens: int = SPEC.max_tokens,
                      question: str = QUESTION) -> tuple[int, int]:
    """(tokens, micros) the router reserves for a seeded conversation under TRUSTED."""
    from gameshark.inference import Message
    prompt = prompt_bytes_bound((Message(Role.SYSTEM, SYSTEM), Message(Role.USER, question)),
                                TRUSTED.per_message, TRUSTED.fixed)
    caps = provider.capabilities()
    micros = -(-prompt * caps.price_in_micros // 1000) + -(-max_tokens * caps.price_out_micros
                                                            // 1000)
    return prompt + max_tokens, micros


def fails(*errors, at_chunk: int = 0) -> dict:
    """Scripts keyed by attempt: attempt i raises errors[i - 1] (None: succeed)."""
    return {i + 1: Script(fault=Fault(at_chunk, e)) if e else Script()
            for i, e in enumerate(errors)}


def scripted(name: str, call_id: str, per_attempt: dict, base=local) -> FakeProvider:
    p = base(name)
    p._scripts.update({(call_id, a): s for a, s in per_attempt.items()})
    return p


def until(predicate, timeout: float = 10.0) -> None:
    """Wait for another thread's progress. Bounded; fails loudly instead of hanging."""
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError("condition not reached")
        time.sleep(0.002)


def thread_journal(sup, clock) -> tuple[sqlite3.Connection, InferenceJournal]:
    conn = connect(sup.db_path)
    return conn, InferenceJournal(conn, AGENT, LeaseManager(conn, AGENT, clock),
                                  sup.generation, clock)


# ---------------------------------------------------------------------- policy and egress
def test_local_only_never_reaches_cloud_even_when_local_fails(tmp_path, clock):
    sup = boot(tmp_path, clock)
    grant(sup, CLOUD_GRANT)
    seed(sup.inference, privacy=Privacy.LOCAL_ONLY)
    lp = scripted("local", "k", fails(lambda: StreamInterrupted("reset")))
    cp = cloud()
    r = router(sup, cfg(lp), cfg(cp))
    with pytest.raises(StreamInterrupted):
        r.complete("k", "c", SPEC, CancelToken())
    assert cp.count() == 0
    assert [c.name for c in r.select("c", SPEC)] == ["local"]

    only_cloud = router(sup, cfg(cp))
    with pytest.raises(EgressDenied):
        only_cloud.complete("k2", "c", SPEC, CancelToken())
    assert cp.count() == 0 and call_row(sup.conn, "k2") is None
    sup.close()


def test_egress_revocation_is_durable_and_stops_cloud(tmp_path, clock):
    sup = boot(tmp_path, clock)
    grant(sup, CLOUD_GRANT)
    seed(sup.inference, "a", Privacy.CLOUD_OK)
    cp = cloud()
    assert router(sup, cfg(cp)).complete("k1", "a", SPEC, CancelToken()).provider == "cloud"
    sup.revoke(GRANT_ADAPTER, CLOUD_GRANT)
    sup.close()

    sup = boot(tmp_path, clock)                     # restart: only durable state remains
    grant(sup, CLOUD_GRANT)                         # re registration never restores it
    seed(sup.inference, "b", Privacy.CLOUD_OK)
    cp = cloud()
    with pytest.raises(EgressDenied):
        router(sup, cfg(cp)).complete("k2", "b", SPEC, CancelToken())
    lp = local()
    got = router(sup, cfg(lp), cfg(cp), policy=RouterPolicy(preference=("cloud",)))
    assert got.complete("k2", "b", SPEC, CancelToken()).provider == "local"
    assert cp.count() == 0
    sup.close()


def test_revocation_after_selection_is_caught_inside_the_reserve_txn(tmp_path, clock):
    sup = boot(tmp_path, clock)
    grant(sup, CLOUD_GRANT)
    seed(sup.inference, privacy=Privacy.CLOUD_OK)
    cp = cloud()
    r = router(sup, cfg(cp))
    select = r.select

    def select_then_revoke(*a, **kw):
        out = select(*a, **kw)
        other = connect(sup.db_path)              # another writer revokes in between
        GrantStore(other, AGENT, clock).revoke(GRANT_ADAPTER, CLOUD_GRANT)
        other.close()
        return out

    r.select = select_then_revoke
    with pytest.raises(EgressDenied):
        r.complete("k", "c", SPEC, CancelToken())
    assert cp.count() == 0
    assert call_row(sup.conn, "k") is None and ledger(sup.conn, "k") == []
    sup.close()


def test_policy_refusal_never_becomes_cloud_fallback(tmp_path, clock):
    sup = boot(tmp_path, clock)
    grant(sup, CLOUD_GRANT, SOFT_BUDGET_GRANT)
    seed(sup.inference, privacy=Privacy.CLOUD_OK)
    lp, cp = local(), cloud()
    tokens, _ = bytes_reservation(lp)
    sup.inference.set_budget(HOUR, tokens - 1, 10**9)  # local's trusted bound does not fit
    # Cloud's whitespace estimate would fit, under the soft grant, but the walk stops.
    r = router(sup, cfg(lp), cfg(cp, UNTRUSTED_BOUND))
    with pytest.raises(BudgetExceeded):
        r.complete("k", "c", SPEC, CancelToken())
    assert lp.count() == 0 and cp.count() == 0
    sup.close()


UNTRUSTED_BOUND = TokenBound(BoundKind.ESTIMATE)


# ---------------------------------------------------------------------- capability and order
def test_capability_constraint_and_context_window_filtering(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    plain, tools = local("plain"), local("tools", features=(Capability.TOOLS,))
    small = local("small", window=60)              # trusted bytes bound + 16 does not fit
    r = router(sup, cfg(plain), cfg(tools), cfg(small))
    names = [c.name for c in r.select("c", SPEC)]
    assert "small" not in names and set(names) == {"plain", "tools"}

    needs_tools = CallSpec(max_tokens=16, needs=frozenset({Capability.TOOLS}))
    assert [c.name for c in r.select("c", needs_tools)] == ["tools"]
    assert r.complete("k", "c", needs_tools, CancelToken()).provider == "tools"
    assert plain.count() == 0

    with pytest.raises(CapabilityUnsupported):
        r.select("c", CallSpec(max_tokens=16, needs=frozenset({Capability.VISION})))
    with pytest.raises(ContextTooLong):
        router(sup, cfg(small)).complete("k2", "c", SPEC, CancelToken())
    assert small.count() == 0 and call_row(sup.conn, "k2") is None

    constrained = router(sup, cfg(plain), cfg(tools),
                         policy=RouterPolicy(allowed=frozenset({"tools"})))
    assert [c.name for c in constrained.select("c", SPEC)] == ["tools"]
    sup.close()


def test_local_first_by_default_and_configured_preference(tmp_path, clock):
    sup = boot(tmp_path, clock)
    grant(sup, CLOUD_GRANT)
    seed(sup.inference, privacy=Privacy.CLOUD_OK)
    cheap, dear, lp = cloud("cheap", price_in=10), cloud("dear", price_in=900), local("lp")
    r = router(sup, cfg(dear), cfg(cheap), cfg(lp))
    assert [c.name for c in r.select("c", SPEC)] == ["lp", "cheap", "dear"]
    pref = router(sup, cfg(dear), cfg(cheap), cfg(lp),
                  policy=RouterPolicy(preference=("dear", "lp")))
    assert [c.name for c in pref.select("c", SPEC)] == ["dear", "lp", "cheap"]
    out = pref.complete("k", "c", SPEC, CancelToken())
    assert (out.provider, out.degraded) == ("dear", False)
    assert lp.count() == 0
    sup.close()


def test_unhealthy_and_rate_limited_providers_are_filtered(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    down = local("down")
    down.health_status = Health(HealthStatus.DOWN)
    now = [0.0]
    health = ProviderHealth(monotonic=lambda: now[0])
    limited = scripted("limited", "k", fails(lambda: RateLimited("429", retry_after=30)))
    ok = local("ok")
    r = router(sup, cfg(down), cfg(limited), cfg(ok), health=health)
    out = r.complete("k", "c", SPEC, CancelToken())
    assert (out.provider, out.degraded) == ("ok", True)
    assert down.count() == 0 and limited.count() == 1
    assert [c.name for c in r.select("c", SPEC)] == ["ok"]          # circuit open
    now[0] = 31.0
    assert [c.name for c in r.select("c", SPEC)] == ["limited", "ok"]
    with pytest.raises(ProviderUnavailable):
        router(sup, cfg(down)).select("c", SPEC)
    sup.close()


# ---------------------------------------------------------------------- failover and attempts
def test_retryable_failure_falls_back_and_records_degraded(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    a = scripted("a", "k", fails(lambda: ModelCrashed("exit 137"), at_chunk=3))
    b = local("b")
    out = router(sup, cfg(a), cfg(b)).complete("k", "c", SPEC, CancelToken())
    assert (out.provider, out.degraded) == ("b", True)
    assert ledger(sup.conn, "k") == [(1, "a", "lost"), (2, "b", "settled")]
    row = call_row(sup.conn, "k")
    assert (row["status"], row["provider"], row["degraded"], row["attempts"]) == (
        "done", "b", 1, 2)
    assert row["error_kind"] is None
    assert answers(sup.conn, "k") == 1
    sup.close()


def test_cancellation_mid_stream_is_terminal_without_fallback(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    token = CancelToken()
    a = local("a")
    a._scripts["k"] = Script(delays=(0.0, 5.0))
    a.sleep = lambda _s, tok: tok.cancel("operator stop")          # cancel lands mid stream
    b = local("b")
    with pytest.raises(ProviderCancelled):
        router(sup, cfg(a), cfg(b)).complete("k", "c", SPEC, token)
    assert b.count() == 0
    assert ledger(sup.conn, "k") == [(1, "a", "lost")]               # dispatched: charged
    assert call_row(sup.conn, "k")["status"] == "pending" and answers(sup.conn, "k") == 0

    sup.inference.cancel("k")                                        # durable operator cancel
    with pytest.raises(CallCancelled):
        router(sup, cfg(b)).complete("k", "c", SPEC, CancelToken())
    assert b.count() == 0
    sup.close()


def test_failure_after_dispatch_charges_and_refusal_before_dispatch_does_not(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference)

    class Liar(FakeProvider):
        def generate(self, req, cancel):
            raise CapabilityUnsupported("declared tools, has none")

    liar = Liar("aliar", local().capabilities())        # sorts first
    b = scripted("b", "k", fails(lambda: Timeout("slow")))
    with pytest.raises(Timeout):
        router(sup, cfg(liar), cfg(b)).complete("k", "c", SPEC, CancelToken())
    assert ledger(sup.conn, "k") == [(1, "b", "lost")]             # liar consumed nothing
    lost = sup.conn.execute("SELECT reserved_micros, cost_micros, reserved_tokens FROM "
                            "budget_ledger WHERE call_id='k'").fetchone()
    assert lost["cost_micros"] == lost["reserved_micros"]
    assert sup.inference.spend()["tokens"] == lost["reserved_tokens"]
    assert call_row(sup.conn, "k")["attempts"] == 1
    sup.close()


def test_at_most_three_attempts_across_restart(tmp_path, clock):
    def providers():
        return [scripted(n, "k", {a: Script(fault=Fault(0, lambda: ModelCrashed("x")))
                                  for a in (1, 2, 3, 4)}) for n in ("a", "b")]

    sup = boot(tmp_path, clock)
    seed(sup.inference)
    first = providers()
    with pytest.raises(ModelCrashed):
        router(sup, *map(cfg, first)).complete("k", "c", SPEC, CancelToken())
    assert sum(p.count() for p in first) == 2
    sup.close()

    sup = boot(tmp_path, clock)
    second = providers()
    r = router(sup, *map(cfg, second))
    with pytest.raises(AttemptsExhausted):          # a runs attempt 3; b finds the cap
        r.complete("k", "c", SPEC, CancelToken())
    with pytest.raises(AttemptsExhausted):
        r.complete("k", "c", SPEC, CancelToken())
    assert (second[0].count(), second[1].count()) == (1, 0)
    assert [x[0] for x in ledger(sup.conn, "k")] == [1, 2, 3]
    assert call_row(sup.conn, "k")["status"] == "failed"
    assert reserved_rows(sup.conn) == 0
    sup.close()


def test_auth_failure_is_terminal_until_config_changes(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    a = scripted("a", "k", fails(lambda: AuthFailed("401")))
    b = local("b")
    r = router(sup, cfg(a, fingerprint="key1"), cfg(b))
    with pytest.raises(AuthFailed):
        r.complete("k", "c", SPEC, CancelToken())
    assert (a.count(), b.count()) == (1, 0)
    assert call_row(sup.conn, "k")["error_kind"] == "AuthFailed:a:key1"
    sup.close()

    sup = boot(tmp_path, clock)                     # restart keeps the block durable
    a2 = local("a")
    with pytest.raises(AuthFailed):
        router(sup, cfg(a2, fingerprint="key1"), cfg(local("b"))).complete(
            "k", "c", SPEC, CancelToken())
    assert a2.count() == 0 and call_row(sup.conn, "k")["attempts"] == 1
    out = router(sup, cfg(a2, fingerprint="key2")).complete("k", "c", SPEC, CancelToken())
    assert out.provider == "a" and a2.count() == 1                 # rotated key: replay runs
    sup.close()


def test_auth_block_keeps_provider_out_of_other_calls(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference, "c1")
    seed(sup.inference, "c2")
    a = scripted("a", "k1", fails(lambda: AuthFailed("401")))
    b = local("b")
    r = router(sup, cfg(a, fingerprint="key1"), cfg(b))
    with pytest.raises(AuthFailed):
        r.complete("k1", "c1", SPEC, CancelToken())
    assert r.complete("k2", "c2", SPEC, CancelToken()).provider == "b"
    assert a.count() == 1
    sup.close()


# ---------------------------------------------------------------------- budgets
def test_budget_exhaustion_refuses_before_dispatch_and_resets(tmp_path, clock):
    sup = boot(tmp_path, clock, ttl=2 * HOUR)       # the lease outlives the window jump
    lp = local()
    reserved, _ = bytes_reservation(lp)
    actual = len(f"{SYSTEM} {QUESTION}".split()) + 8               # settled usage per call
    sup.inference.set_budget(HOUR, 2 * actual + reserved - 1, 0)
    r = router(sup, cfg(lp))
    for i in range(3):
        seed(sup.inference, f"c{i}")
    r.complete("k0", "c0", SPEC, CancelToken())
    r.complete("k1", "c1", SPEC, CancelToken())
    with pytest.raises(BudgetExceeded) as err:
        r.complete("k2", "c2", SPEC, CancelToken())
    assert err.value.reset_at == window_start(clock.now(), HOUR) + HOUR
    assert lp.count() == 2 and call_row(sup.conn, "k2") is None     # no attempt consumed

    clock.advance(err.value.reset_at - clock.now())
    assert r.complete("k2", "c2", SPEC, CancelToken()).provider == "local"
    assert call_row(sup.conn, "k2")["attempts"] == 1

    seed(sup.inference, "huge", question="x " * 5000)
    wide = router(sup, cfg(local("wide", window=100_000)))
    with pytest.raises(BudgetExceeded) as err:
        wide.complete("k3", "huge", SPEC, CancelToken())
    assert err.value.reset_at == math.inf                           # can never fit
    sup.close()


def test_lost_and_outstanding_reservations_count_across_restart(tmp_path, clock):
    sup = boot(tmp_path, clock)
    lp = local()
    reserved, _ = bytes_reservation(lp)
    sup.inference.set_budget(HOUR, 2 * reserved, 0)
    seed(sup.inference, "c0")
    seed(sup.inference, "c1")
    # An attempt reserved, then the process died before dispatch.
    sup.inference.reserve("k0", "c0", "local", lp.capabilities(), SPEC,
                          prompt_tokens=TRUSTED.prompt)
    sup.close()

    sup = boot(tmp_path, clock)                     # recovery settles it lost, in full
    assert ledger(sup.conn, "k0") == [(1, "local", "lost")]
    assert sup.inference.spend()["tokens"] == reserved
    r = router(sup, cfg(local()))
    r.complete("k1", "c1", SPEC, CancelToken())     # fits: one reservation left
    with pytest.raises(BudgetExceeded):
        r.complete("k0", "c0", SPEC, CancelToken())  # lost plus settled leave no room
    sup.close()


def test_concurrent_admissions_never_oversubscribe_the_final_reservation(tmp_path, clock):
    sup = boot(tmp_path, clock)
    grant(sup, CLOUD_GRANT)
    n, fit = 8, 3
    probe = cloud()
    _, micros = bytes_reservation(probe)
    sup.inference.set_budget(HOUR, 10**9, fit * micros + micros // 2)
    for i in range(n):
        seed(sup.inference, f"c{i}", Privacy.CLOUD_OK)
    gate = threading.Event()
    cp = cloud(slots=n, sleep=lambda _s, _t: gate.wait(10))
    cp._scripts.update({f"k{i}": Script(delays=(0.001,)) for i in range(n)})
    scheduler, health = Scheduler(), ProviderHealth()
    barrier, out = threading.Barrier(n), {}

    def worker(i: int) -> None:
        conn, journal = thread_journal(sup, clock)
        r = InferenceRouter(journal, [cfg(cp)], policy=RouterPolicy(agent_in_flight=n),
                            scheduler=scheduler, health=health)
        barrier.wait()
        try:
            out[i] = r.complete(f"k{i}", f"c{i}", SPEC, CancelToken())
        except ProviderError as exc:
            out[i] = exc
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    until(lambda: cp.in_flight + sum(isinstance(v, BudgetExceeded) for v in out.values()) == n)
    held = sup.inference.spend()
    assert cp.in_flight == fit and held["micros"] == fit * micros   # all held at once
    gate.set()
    for t in threads:
        t.join(timeout=20)
    refused = [i for i, v in out.items() if isinstance(v, BudgetExceeded)]
    assert len(refused) == n - fit and cp.count() == fit
    assert sup.inference.spend()["micros"] <= fit * micros + micros // 2
    assert reserved_rows(sup.conn) == 0
    sup.close()


def test_untrusted_paid_cloud_needs_soft_grant_and_reports_overrun(tmp_path, clock):
    sup = boot(tmp_path, clock)
    grant(sup, CLOUD_GRANT)
    for i in range(3):
        seed(sup.inference, f"c{i}", Privacy.CLOUD_OK)
    cp = cloud(price_in=1000, price_out=0)
    cp._scripts["k0"] = Script(usage=Usage(tokens_in=500, tokens_out=8))  # real tokenizer
    sup.inference.set_budget(HOUR, 10**9, 600)
    r = router(sup, cfg(cp, UNTRUSTED_BOUND))
    with pytest.raises(BudgetUnenforceable):
        r.complete("k0", "c0", SPEC, CancelToken())
    assert cp.count() == 0

    grant(sup, SOFT_BUDGET_GRANT)
    out = r.complete("k0", "c0", SPEC, CancelToken())
    assert out.soft_budget and out.overrun is not None and not out.overrun.hard
    row = sup.conn.execute("SELECT * FROM budget_ledger WHERE call_id='k0'").fetchone()
    assert (row["tokens_in"], row["cost_micros"]) == (500, 500)      # actual, not truncated
    assert row["cost_micros"] > row["reserved_micros"]
    assert [r_["call_id"] for r_ in sup.inference.overruns()] == ["k0"]
    # Admission now sees the actual spend: 500 of 600 used, so an 11 micro estimate fits
    # and a 207 micro one does not.
    r.complete("k1", "c1", SPEC, CancelToken())
    seed(sup.inference, "big", Privacy.CLOUD_OK, question="y " * 200)
    with pytest.raises(BudgetExceeded):
        r.complete("k2", "big", SPEC, CancelToken())
    sup.close()


def test_trusted_bound_violation_is_hard_overrun_and_demotes(tmp_path, clock):
    sup = boot(tmp_path, clock)
    grant(sup, CLOUD_GRANT)
    seed(sup.inference, "c0", Privacy.CLOUD_OK)
    seed(sup.inference, "c1", Privacy.CLOUD_OK)
    cp = cloud()
    cp._scripts["k0"] = Script(usage=Usage(tokens_in=10_000, tokens_out=8))
    sup.inference.set_budget(HOUR, 10**9, 10**9)
    r = router(sup, cfg(cp))
    out = r.complete("k0", "c0", SPEC, CancelToken())
    assert out.overrun is not None and out.overrun.hard and not out.soft_budget
    assert out.overrun.actual_tokens == 10_008
    with pytest.raises(BudgetUnenforceable):        # demoted: no longer a trusted bound
        r.complete("k1", "c1", SPEC, CancelToken())
    sup.close()


def test_local_untrusted_runs_soft_without_a_grant(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    sup.inference.set_budget(HOUR, 10**6, 0)
    out = router(sup, cfg(local(), UNTRUSTED_BOUND)).complete("k", "c", SPEC, CancelToken())
    assert out.soft_budget and out.overrun is None
    sup.close()


# ---------------------------------------------------------------------- slots and queues
def _hold(scheduler: Scheduler, names, out: list, release: threading.Event, tag,
          cancel: CancelToken | None = None, wait: float = 10.0) -> threading.Thread:
    def body() -> None:
        try:
            slot = scheduler.acquire(names, cancel, wait)
        except ProviderError as exc:
            out.append((tag, exc))
            return
        out.append((tag, slot.provider))
        release.wait(10)
        slot.release()

    t = threading.Thread(target=body)
    t.start()
    return t


def test_fifo_queue_capacity_and_slot_release():
    s = Scheduler()
    s.configure("p", slots=1, queue_depth=2)
    order: list = []
    gates = {k: threading.Event() for k in ("h", "w1", "w2")}
    holder = _hold(s, ["p"], order, gates["h"], "h")
    until(lambda: s.stats()["p"].in_flight == 1)
    waiters = []
    for k in ("w1", "w2"):
        waiters.append(_hold(s, ["p"], order, gates[k], k))
        until(lambda k=k: s.stats()["p"].queued == int(k[1]))
    with pytest.raises(Overloaded) as err:
        s.acquire(["p"], None, 1.0)                 # queue full: refused at once
    assert err.value.retry_after > 0
    gates["h"].set()
    until(lambda: len(order) == 2)
    gates["w1"].set()
    until(lambda: len(order) == 3)
    gates["w2"].set()
    for t in (holder, *waiters):
        t.join(timeout=10)
    assert [tag for tag, _ in order] == ["h", "w1", "w2"]          # strict FIFO
    st = s.stats()["p"]
    assert (st.in_flight, st.queued, st.peak_in_flight, st.peak_queue) == (0, 0, 1, 2)


def test_queue_timeout_and_cancel_leave_no_slot_or_waiter():
    s = Scheduler()
    s.configure("p", slots=1, queue_depth=4)
    gate, out = threading.Event(), []
    holder = _hold(s, ["p"], out, gate, "h")
    until(lambda: s.stats()["p"].in_flight == 1)
    with pytest.raises(Overloaded):
        s.acquire(["p"], None, 0.05)               # bounded wait, then refused
    assert s.stats()["p"].queued == 0
    token = CancelToken()
    cancelled = _hold(s, ["p"], out, threading.Event(), "c", cancel=token, wait=30)
    until(lambda: s.stats()["p"].queued == 1)
    t0 = time.monotonic()
    token.cancel("operator")
    cancelled.join(timeout=5)
    assert time.monotonic() - t0 < 1.0             # woken by the cancel, not the timeout
    assert isinstance(out[-1][1], ProviderCancelled)
    gate.set()
    holder.join(timeout=5)
    assert s.stats()["p"].in_flight == 0 and s.stats()["p"].queued == 0
    with pytest.raises(ProviderCancelled):
        s.acquire(["p"], token, 1.0)               # a cancelled token never takes a slot


def test_free_slot_on_later_candidate_and_call_limits():
    s = Scheduler()
    s.configure("a", slots=1)
    s.configure("b", slots=1)
    first = s.acquire(["a", "b"])
    second = s.acquire(["a", "b"])
    assert (first.provider, second.provider) == ("a", "b")
    with pytest.raises(Overloaded):
        s.acquire(["a", "b"])                      # no wait configured: fail fast
    first.release()
    first.release()                                # idempotent
    second.release()
    assert all(v.in_flight == 0 for v in s.stats().values())

    t1 = s.admit_call("stark", "conv", agent_limit=2, conversation_limit=1)
    with pytest.raises(Overloaded):
        s.admit_call("stark", "conv", agent_limit=2, conversation_limit=1)
    t2 = s.admit_call("stark", "conv2", agent_limit=2, conversation_limit=1)
    with pytest.raises(Overloaded):
        s.admit_call("stark", "conv3", agent_limit=2, conversation_limit=1)
    t1.release()
    t2.release()
    assert s.calls_in_flight() == ({}, {})


def test_router_releases_slots_and_tickets_on_every_exit(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    s = Scheduler()
    a = scripted("a", "k", fails(lambda: StreamInterrupted("x"), lambda: InvalidRequest("y")))
    r = router(sup, cfg(a), scheduler=s)
    for err in (StreamInterrupted, InvalidRequest):
        with pytest.raises(err):
            r.complete("k", "c", SPEC, CancelToken())
        assert s.stats()["a"].in_flight == 0 and s.calls_in_flight() == ({}, {})
    token = CancelToken()
    token.cancel("stop")
    with pytest.raises(ProviderCancelled):
        r.complete("k", "c", SPEC, token)
    assert s.stats()["a"].in_flight == 0 and s.calls_in_flight() == ({}, {})
    assert call_row(sup.conn, "k")["attempts"] == 2                # queued cancel: no attempt
    sup.close()


def test_same_conversation_second_call_is_overloaded(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    s, gate = Scheduler(), threading.Event()
    a = local("a")
    a.sleep = lambda _s, _t: gate.wait(10)
    a._scripts["k1"] = Script(delays=(0.001,))
    r = router(sup, cfg(a), scheduler=s)
    out = {}

    def first() -> None:
        conn, journal = thread_journal(sup, clock)
        try:
            out["k1"] = InferenceRouter(journal, [cfg(a)], scheduler=s).complete(
                "k1", "c", SPEC, CancelToken())
        finally:
            conn.close()

    t = threading.Thread(target=first)
    t.start()
    until(lambda: a.in_flight == 1)
    with pytest.raises(Overloaded):
        r.complete("k2", "c", SPEC, CancelToken())
    gate.set()
    t.join(timeout=10)
    assert out["k1"].committed.seq == 2 and call_row(sup.conn, "k2") is None
    sup.close()


# ---------------------------------------------------------------------- journal and identity
class RoutedAssistant(AssistantAdapter):
    """The assistant adapter with its single provider replaced by a router."""

    def __init__(self, configs, remote: GameSimulator) -> None:
        super().__init__(configs[0].provider, remote)
        self.configs = configs

    def on_event(self, event: dict) -> None:
        ctx = self.ctx
        topic = event["payload"]["topic"]
        cid, rid, call_id = f"conv:{topic}", f"ask:{topic}", call_id_for(event["event_id"])
        ctx.responsibilities.upsert(rid, kind="assistant", payload={"topic": topic},
                                    state="active")
        j = ctx.inference
        j.open_conversation(cid, Privacy.LOCAL_ONLY, responsibility_id=rid)
        j.append(cid, Role.SYSTEM, SYSTEM, 0)
        j.append(cid, Role.USER, event["payload"]["question"], 1)
        try:
            out = InferenceRouter(j, self.configs).complete(call_id, cid, self.spec, ctx.cancel)
        except ProviderError as exc:
            # Retryable failures leave the duty active for its owner to reschedule; terminal
            # ones block it. Neither completes it.
            state = "active" if exc.retryable else "blocked"
            ctx.responsibilities.transition(rid, state, checkpoint=type(exc).__name__)
            self.outcomes.append(type(exc).__name__)
            return
        self.outcomes.append("replayed" if out.committed.replayed else "committed")
        ctx.emit_effect(self._effect(call_id, cid, out.committed.content, rid))
        ctx.responsibilities.transition(rid, "done")


def boot_routed(root: Path, clock, configs) -> AgentSupervisor:
    sup = AgentSupervisor(db_path=root / "runtime.db", agent_id=AGENT, owner_token=HOST,
                          clock=clock)
    sup.add_adapter(RoutedAssistant(configs, GameSimulator(root / "game.db")))
    sup.start()
    return sup


def test_routed_answer_keeps_identity_and_commits_once(tmp_path, clock):
    call = call_id_for(ask_event()["event_id"])
    a = scripted("a", call, fails(lambda: ModelCrashed("x")))
    sup = boot_routed(tmp_path, clock, [cfg(a), cfg(local("b"))])
    uuid = sup.identity.uuid
    assert sup.submit(ask_event()) == "acked"
    assert answers(sup.conn, call) == 1 and sup.responsibility_state("ask:t1") == "done"
    sup.close()

    again = boot_routed(tmp_path, clock, [cfg(local("b")), cfg(local("a"))])  # swapped order
    assert again.identity.uuid == uuid
    assert again.submit(ask_event()) == "duplicate"
    assert answers(again.conn, call) == 1
    row = call_row(again.conn, call)
    assert (row["provider"], row["degraded"]) == ("b", 1)
    again.close()


def test_provider_failure_never_completes_the_responsibility(tmp_path, clock):
    call = call_id_for(ask_event()["event_id"])
    a = scripted("a", call, fails(lambda: StreamInterrupted("x")))
    sup = boot_routed(tmp_path, clock, [cfg(a)])
    assert sup.submit(ask_event()) == "acked"
    assert sup.responsibility_state("ask:t1") == "active"
    assert answers(sup.conn, call) == 0 and sup.effect_status(f"reply:{call}") is None
    sup.close()

    terminal = call_id_for(ask_event(topic="t2")["event_id"])
    b = scripted("a", terminal, fails(lambda: InvalidRequest("bad")))
    sup = boot_routed(tmp_path, clock, [cfg(b)])
    assert sup.submit(ask_event(topic="t2")) == "acked"
    assert sup.responsibility_state("ask:t2") == "blocked"
    assert answers(sup.conn, terminal) == 0
    sup.close()


# ---------------------------------------------------------------------- soak
def test_one_thousand_randomized_calls_leak_nothing(tmp_path):
    clock = FakeClock()
    sup = boot(tmp_path, clock)
    grant(sup, CLOUD_GRANT)
    rng = random.Random(1234)
    n_calls, n_threads = 1000, 8
    errors = [lambda: StreamInterrupted("reset"), lambda: ModelCrashed("exit"),
              lambda: Timeout("slow"), lambda: RateLimited("429", retry_after=0.0),
              lambda: ProviderCancelled("operator"), lambda: InvalidRequest("bug")]
    weights = [30, 20, 15, 10, 8, 2]   # AuthFailed blocks a provider process wide; tested alone
    providers = [local("la", slots=2), local("lb", slots=1), cloud("cc", slots=2)]
    configs = [cfg(p, queue_depth=3) for p in providers]
    calls = []
    for i in range(n_calls):
        privacy = Privacy.CLOUD_OK if i % 2 else Privacy.LOCAL_ONLY
        seed(sup.inference, f"c{i}", privacy, question=f"q{i} " * rng.randint(1, 20))
        for p in providers:
            for attempt in (1, 2, 3):
                if rng.random() < 0.25:
                    err = rng.choices(errors, weights)[0]
                    p._scripts[(f"k{i}", attempt)] = Script(
                        fault=Fault(rng.randint(0, 8), err),
                        delays=tuple(rng.choice((0.0, 0.0005)) for _ in range(3)))
        calls.append(i)
    precancel = set(rng.sample(calls, 20))
    scheduler, health = Scheduler(), ProviderHealth()
    policy = RouterPolicy(max_queue_wait_s=0.05, agent_in_flight=n_threads)
    work = list(calls)
    lock = threading.Lock()
    outcomes: dict[int, str] = {}

    def worker() -> None:
        conn, journal = thread_journal(sup, clock)
        r = InferenceRouter(journal, configs, policy=policy, scheduler=scheduler, health=health)
        try:
            while True:
                with lock:
                    if not work:
                        return
                    i = work.pop()
                token = CancelToken()
                if i in precancel:
                    token.cancel("operator")
                for _ in range(12):                 # owner replays until a terminal answer
                    try:
                        r.complete(f"k{i}", f"c{i}", SPEC, token)
                        outcomes[i] = "done"
                        break
                    except (Overloaded, ProviderUnavailable) as exc:
                        outcomes[i] = "overloaded"
                        time.sleep(min(getattr(exc, "retry_after", 0.0), 0.002))
                    except ProviderError as exc:
                        outcomes[i] = type(exc).__name__
                        if not exc.retryable or isinstance(exc, ProviderCancelled):
                            break
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)
    assert not any(t.is_alive() for t in threads)
    assert len(outcomes) == n_calls

    stats = scheduler.stats()
    for p, c in zip(providers, configs, strict=True):
        st = stats[p.name]
        assert (st.in_flight, st.queued) == (0, 0), (p.name, st)      # zero leaked slots
        assert st.peak_in_flight <= st.slots and st.peak_queue <= c.queue_depth
        assert p.peak_in_flight <= st.slots and p.in_flight == 0
    assert scheduler.calls_in_flight() == ({}, {})

    conn = sup.conn
    assert reserved_rows(conn) == 0
    assert conn.execute("SELECT MAX(attempts) FROM inference_calls").fetchone()[0] <= MAX_ATTEMPTS
    assert conn.execute("SELECT COUNT(*) FROM budget_ledger WHERE attempt > ?",
                        (MAX_ATTEMPTS,)).fetchone()[0] == 0
    dup = conn.execute("SELECT call_id, COUNT(*) FROM messages WHERE call_id IS NOT NULL "
                       "GROUP BY call_id HAVING COUNT(*) > 1").fetchall()
    assert dup == []
    done = {r[0] for r in conn.execute("SELECT call_id FROM inference_calls WHERE status='done'")}
    for i, outcome in outcomes.items():
        assert (outcome == "done") == (f"k{i}" in done), (i, outcome)
        if f"k{i}" in done:
            assert answers(conn, f"k{i}") == 1
    cloud_local_only = [r for r in providers[2].invocations
                        if r.privacy is Privacy.LOCAL_ONLY]
    assert cloud_local_only == []                                    # R7 under load
    tally = Counter(outcomes.values())
    assert tally["done"] > n_calls // 2, tally
    sup.close()
