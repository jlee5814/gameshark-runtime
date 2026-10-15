"""Model lifecycle tracker: lazy readiness, crash versus request failure, bounded recovery,
cooldown, cancellation, replacement, and restart.

Policy time is a manual clock: plan delays advance it instead of sleeping, so every
cooldown and verify TTL is exact. Threads that race use threading.Event gates and their own
SQLite connections, the shape of the worker executor in increment 6. HTTP cases run against
the loopback OpenAI fixture or a refused port. A restart discards the supervisor, router,
tracker, scheduler, and providers.
"""

from __future__ import annotations

import random
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.inference import (
    CLOUD_GRANT,
    Capabilities,
    EgressDenied,
    FakeProvider,
    Fault,
    Health,
    HealthStatus,
    IllegalTransition,
    InferenceRouter,
    InvalidRequest,
    LoadPlan,
    Locality,
    ModelCrashed,
    ModelState,
    ModelTracker,
    ModelUnavailable,
    OpenAICompatProvider,
    Privacy,
    ProviderCancelled,
    ProviderError,
    ProviderHealth,
    ProviderUnavailable,
    RateLimited,
    Readiness,
    Role,
    RouterPolicy,
    Scheduler,
    Script,
    StreamInterrupted,
    Timeout,
    TrackerPolicy,
)
from gameshark.inference.types import ContextTooLong
from gameshark.lifecycle import CancelToken
from tests.openai_fixture import OpenAIFixture, Reply, refused_port
from tests.test_inference_router import (
    SPEC,
    answers,
    boot,
    call_row,
    cfg,
    grant,
    ledger,
    seed,
    thread_journal,
    until,
)

D, LO, R, E, I = (ModelState.DISCOVERED, ModelState.LOADING, ModelState.READY,
                  ModelState.EXECUTING, ModelState.IDLE)
UNLOADING, UNLOADED, CRASHED, FAILED = (ModelState.UNLOADING, ModelState.UNLOADED,
                                        ModelState.CRASHED, ModelState.FAILED)
OK = Health(HealthStatus.OK)
UNLISTED = Health(HealthStatus.DEGRADED, "model not listed")
DOWN = Health(HealthStatus.DOWN, "connection refused")


# ---------------------------------------------------------------------- helpers
class Time:
    """Manual policy clock. Plan delays advance it; nothing sleeps."""

    def __init__(self) -> None:
        self.t = 0.0
        self.waits: list[float] = []

    def now(self) -> float:
        return self.t

    def wait(self, token: CancelToken, seconds: float) -> bool:
        self.waits.append(seconds)
        self.t += seconds
        return token.cancelled


def fake(name: str = "local", slots: int = 2, window: int = 4096, locality=Locality.LOCAL,
         price: int = 0, **kw) -> FakeProvider:
    return FakeProvider(name, Capabilities(f"{name}-model", locality, window,
                                           max_concurrency=slots, price_in_micros=price,
                                           price_out_micros=price), **kw)


def tracker(policy: TrackerPolicy | None = None, t: Time | None = None
            ) -> tuple[ModelTracker, Time]:
    t = t or Time()
    return ModelTracker(Scheduler(monotonic=t.now), ProviderHealth(monotonic=t.now),
                        policy or TrackerPolicy(), monotonic=t.now, wait=t.wait), t


def routed(sup, tr: ModelTracker, *configs, journal=None, **policy) -> InferenceRouter:
    policy.setdefault("agent_in_flight", 16)
    return InferenceRouter(journal or sup.inference, configs, policy=RouterPolicy(**policy),
                           tracker=tr)


def crash_script(*call_ids: str, error=lambda: ModelCrashed("exit 137"), at: int = 0):
    return {k: Script(fault=Fault(at, error)) for k in call_ids}


def run(r: InferenceRouter, call_id: str, conv: str = "c", cancel: CancelToken | None = None):
    return r.complete(call_id, conv, SPEC, cancel or CancelToken())


def thread_run(fn, out: list) -> threading.Thread:
    def body() -> None:
        try:
            out.append(fn())
        except BaseException as exc:
            out.append(exc)

    t = threading.Thread(target=body, daemon=True)
    t.start()
    return t


def spawn(sup, clock, fn, out: list) -> threading.Thread:
    """Run fn(journal) on a thread with its own SQLite connection, as the executor will."""

    def with_journal():
        conn, j = thread_journal(sup, clock)
        try:
            return fn(j)
        finally:
            conn.close()

    return thread_run(with_journal, out)


def rows(conn, table: str) -> list[tuple]:
    return [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]


# ---------------------------------------------------------------------- construction
def test_models_start_discovered_and_nothing_probes_before_demand(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    tr, _ = tracker()
    a, b = fake("a"), fake("b")
    r = routed(sup, tr, cfg(a), cfg(b))
    assert [tr.state(n) for n in ("a", "b")] == [D, D]
    assert [c.name for c in r.select("c", SPEC)] == ["a", "b"]   # discovered stays eligible
    assert a.health_checks == b.health_checks == 0
    out = run(r, "k")
    assert out.provider == "a" and b.health_checks == 0
    assert tr.path("a") == [D, LO, R, E, I] and tr.state("b") is D
    assert call_row(sup.conn, "k")["instance_ref"] == "a:1.1"
    sup.close()


def test_transition_table_plans_and_wiring_are_validated():
    tr, _ = tracker()
    p = fake()
    tr.register(cfg(p))
    rec = tr._active["local"]
    with tr._cond, pytest.raises(IllegalTransition):
        tr._move(rec, ModelState.IDLE, "skip ahead")
    with pytest.raises(ValueError, match="1 to 3"):
        TrackerPolicy(plans=tuple(LoadPlan(f"p{i}", float(i + 1)) for i in range(4)))
    with pytest.raises(ValueError, match="changes nothing"):
        TrackerPolicy(plans=(LoadPlan("a", 1.0), LoadPlan("b", 1.0)))
    with pytest.raises(ValueError, match="replace it"):
        tr.register(cfg(fake()))                                  # another config, same name
    tr.register(tr.config("local"))                               # the same config: no op


# ---------------------------------------------------------------------- lazy readiness
def test_concurrent_first_demand_loads_once(tmp_path, clock):
    sup = boot(tmp_path, clock)
    gate = threading.Event()
    p = fake(slots=8, on_health=lambda _t: gate.wait(5))
    tr, _ = tracker()
    for i in range(8):
        seed(sup.inference, f"c{i}")
    out: list = []
    threads = [spawn(sup, clock, lambda j, i=i: run(routed(sup, tr, cfg(p), journal=j),
                                                    f"k{i}", f"c{i}"), out)
               for i in range(8)]
    until(lambda: (s := tr.scheduler.stats().get("local")) is not None
          and s.in_flight == 8)                                 # all 8 inside admission
    assert tr.state("local") is LO and p.health_checks == 1
    gate.set()
    for t in threads:
        t.join(10)
    assert all(not isinstance(o, BaseException) for o in out) and len(out) == 8
    assert p.health_checks == 1 and p.count() == 8
    path = tr.path("local")
    assert path[:4] == [D, LO, R, E] and set(path[3:]) <= {E, I} and tr.state("local") is I
    sup.close()


def test_ready_is_not_resident_until_a_token_streams(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    seed(sup.inference, "c2")
    p = fake(scripts=crash_script("k1", error=lambda: InvalidRequest("bad")))
    tr, _ = tracker()
    r = routed(sup, tr, cfg(p))
    with pytest.raises(InvalidRequest):
        run(r, "k1")
    v = tr.view("local")
    assert (v.state, v.readiness) == (R, Readiness.LISTED)      # listed, never served a token
    run(r, "k2", "c2")
    v = tr.view("local")
    assert (v.state, v.readiness) == (I, Readiness.RESIDENT)
    assert tr.path("local") == [D, LO, R, E, R, E, I]
    sup.close()


def test_unlisted_http_model_loads_only_through_the_endpoint_only_plan(tmp_path, clock):
    srv = OpenAIFixture()
    srv.routes["/v1/models"] = Reply(headers={}, parts=[b'{"data": [{"id": "other"}]}'])
    try:
        sup = boot(tmp_path, clock)
        seed(sup.inference)
        tr, t = tracker()
        p = OpenAICompatProvider("lms", f"{srv.base}/v1", "m", 4096)
        r = routed(sup, tr, cfg(p))
        out = run(r, "k")
        assert out.committed.content == "hello world"
        assert t.waits == [2.0, 2.0] and tr.view("lms").probes == 3
        reasons = [h.reason for h in tr.history("lms")]
        assert reasons[1].startswith("plan probe failed (endpoint: model m not listed)")
        assert reasons[3] == "plan endpoint_only passed at endpoint"
        assert tr.view("lms").readiness is Readiness.RESIDENT   # the first token proved it
        sup.close()
    finally:
        srv.stop()


# ---------------------------------------------------------------------- health transitions
def test_verify_ttl_reprobes_and_detects_external_eviction(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    seed(sup.inference, "c2")
    p = fake(health_script=[OK, UNLISTED, UNLISTED, UNLISTED, UNLISTED])
    tr, t = tracker()
    r = routed(sup, tr, cfg(p))
    run(r, "k1")
    t.t += 29.0
    assert tr.state("local") is I and p.health_checks == 1     # inside the TTL: no probe
    t.t += 1.0
    run(r, "k2", "c2")
    assert p.health_checks == 5
    assert tr.path("local") == [D, LO, R, E, I, UNLOADED, LO, LO, LO, R, E, I]
    assert "no longer listed" in tr.history("local")[4].reason
    assert tr.view("local").incarnation == 2
    sup.close()


def test_unavailable_http_endpoint_fails_after_three_plans_with_zero_requests(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    tr, t = tracker()
    gone = OpenAICompatProvider("gone", f"http://127.0.0.1:{refused_port()}/v1", "m", 4096)
    r = routed(sup, tr, cfg(gone))
    with pytest.raises(ModelUnavailable) as info:
        run(r, "k")
    assert info.value.retry_after == 300.0
    assert info.value.retryable and info.value.before_dispatch
    assert tr.path("gone") == [D, LO, LO, LO, FAILED]
    assert t.waits == [2.0, 2.0] and tr.view("gone").probes == 3
    assert call_row(sup.conn, "k") is None and ledger(sup.conn, "k") == []   # no attempt
    t.t += 100.0
    with pytest.raises(ModelUnavailable) as info:
        r.select("c", SPEC)                                     # cooling: filtered, no probe
    assert info.value.retry_after == 200.0 and tr.view("gone").probes == 3
    tr.close()
    sup.close()


def test_cooldown_elapses_then_the_model_loads_again(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    p = fake(health_script=[DOWN, DOWN, DOWN])
    ok = fake("other")
    tr, t = tracker()
    r = routed(sup, tr, cfg(p), cfg(ok))
    out = run(r, "k1")
    assert (out.provider, out.degraded) == ("other", True)      # failover, no attempt on p
    assert ledger(sup.conn, "k1") == [(1, "other", "settled")]
    assert tr.state("local") is FAILED and p.count() == 0
    t.t += 300.0
    assert tr.eligible("local") and tr.state("local") is D
    seed(sup.inference, "c2")
    assert run(r, "k2", "c2").provider == "local"
    assert tr.path("local") == [D, LO, LO, LO, FAILED, D, LO, R, E, I]
    sup.close()


# ---------------------------------------------------------------------- crash versus failure
def test_request_failures_never_suspect_the_model(tmp_path, clock):
    sup = boot(tmp_path, clock)
    errors = {"k1": lambda: Timeout("slow"), "k2": lambda: InvalidRequest("bad"),
              "k3": lambda: RateLimited("429", retry_after=5)}
    p = fake(scripts={k: Script(fault=Fault(1, e)) for k, e in errors.items()})
    tr, _ = tracker()
    r = routed(sup, tr, cfg(p))
    for i, (k, e) in enumerate(errors.items()):
        seed(sup.inference, f"c{i}")
        with pytest.raises(type(e())):
            run(r, k, f"c{i}")
    v = tr.view("local")
    assert (v.suspect, v.request_failures, v.transport_failures, v.crashes) == (None, 3, 0, 0)
    assert p.health_checks == 1 and v.state is I and v.incarnation == 1
    sup.close()


def test_transport_failure_is_suspected_then_cleared_by_a_probe(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    seed(sup.inference, "c2")
    p = fake(scripts=crash_script("k1", error=lambda: StreamInterrupted("reset"), at=2))
    tr, _ = tracker()
    r = routed(sup, tr, cfg(p))
    with pytest.raises(StreamInterrupted):
        run(r, "k1")
    v = tr.view("local")
    assert v.state is I and v.suspect.startswith("StreamInterrupted") and v.crashes == 0
    run(r, "k2", "c2")
    v = tr.view("local")
    assert (v.suspect, v.incarnation, v.crashes, p.health_checks) == (None, 1, 0, 2)
    assert call_row(sup.conn, "k2")["instance_ref"] == "local:1.1"   # same incarnation
    assert CRASHED not in tr.path("local")
    sup.close()


def test_confirmed_crash_rematerializes_a_new_incarnation(tmp_path, clock):
    sup = boot(tmp_path, clock)
    for c in ("c0", "c1", "c2"):
        seed(sup.inference, c)
    p = fake(health_script=[OK, DOWN, OK], scripts=crash_script("k1", at=1))
    tr, _ = tracker()
    r = routed(sup, tr, cfg(p))
    run(r, "k0", "c0")
    with pytest.raises(ModelCrashed):
        run(r, "k1", "c1")
    assert tr.state("local") is I                               # suspected, not yet crashed
    run(r, "k2", "c2")
    assert tr.path("local") == [D, LO, R, E, I, E, I, CRASHED, LO, R, E, I]
    assert [call_row(sup.conn, k)["instance_ref"] for k in ("k0", "k2")] == [
        "local:1.1", "local:1.2"]
    assert tr.view("local").crashes == 1
    sup.close()


def test_http_server_gone_is_suspected_then_crashed_then_failed(tmp_path, clock):
    srv = OpenAIFixture()
    sup = boot(tmp_path, clock)
    for c in ("c0", "c1", "c2"):
        seed(sup.inference, c)
    tr, _ = tracker()
    p = OpenAICompatProvider("sglang", f"{srv.base}/v1", "m", 4096)
    r = routed(sup, tr, cfg(p))
    run(r, "k0", "c0")
    srv.stop()
    with pytest.raises(ProviderUnavailable):
        run(r, "k1", "c1")                                      # a request failed
    assert tr.state("sglang") is I and tr.view("sglang").suspect is not None
    with pytest.raises(ModelUnavailable):
        run(r, "k2", "c2")                                      # the probe confirms
    assert tr.path("sglang")[-5:] == [CRASHED, LO, LO, LO, FAILED]
    assert ledger(sup.conn, "k1") == [(1, "sglang", "lost")] and ledger(sup.conn, "k2") == []
    tr.close()
    sup.close()


# ---------------------------------------------------------------------- bounded recovery
def test_crash_loop_stops_at_max_recoveries_and_success_resets(tmp_path, clock):
    sup = boot(tmp_path, clock)
    ids = [f"k{i}" for i in range(5)]
    for i in range(5):
        seed(sup.inference, f"c{i}")
    p = fake(health_script=[OK, DOWN, OK, DOWN, OK, DOWN],
             scripts=crash_script("k0", "k1", "k2"))
    tr, _ = tracker()
    r = routed(sup, tr, cfg(p))
    for i in range(3):
        with pytest.raises(ModelCrashed):
            run(r, ids[i], f"c{i}")
    with pytest.raises(ModelUnavailable):
        run(r, "k3", "c3")                                      # third crash cycle
    v = tr.view("local")
    assert (v.state, v.crashes, v.recoveries) == (FAILED, 3, 3)
    assert "3 crash cycles" in tr.history("local")[-1].reason

    q = fake("q", health_script=[OK, DOWN, OK], scripts=crash_script("j0", "j2"))
    seed(sup.inference, "d0")
    seed(sup.inference, "d1")
    seed(sup.inference, "d2")
    rq = routed(sup, tr, cfg(q))
    with pytest.raises(ModelCrashed):
        run(rq, "j0", "d0")
    run(rq, "j1", "d1")                                         # verify down, reload, success
    assert tr.view("q").recoveries == 0
    sup.close()


# ---------------------------------------------------------------------- cancellation
def test_cancel_while_waiting_for_a_load_costs_nothing_and_the_load_finishes(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    seed(sup.inference, "c2")
    gate = threading.Event()
    p = fake(on_health=lambda _t: gate.wait(5))
    tr, _ = tracker()
    token, out = CancelToken(), []
    t = spawn(sup, clock, lambda j: run(routed(sup, tr, cfg(p), journal=j), "k1", "c", token),
              out)
    until(lambda: p.health_checks == 1)
    token.cancel("operator stop")
    t.join(5)
    assert isinstance(out[0], ProviderCancelled)
    assert call_row(sup.conn, "k1") is None and tr.state("local") is LO
    gate.set()
    run(routed(sup, tr, cfg(p)), "k2", "c2")
    assert p.health_checks == 1 and p.count() == 1             # the same load served k2
    sup.close()


def test_operator_cancel_mid_call_is_terminal_and_leaves_the_model_healthy(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    p = fake(scripts={"k": Script(delays=(0.0, 30.0))})
    tr, _ = tracker()
    token, out = CancelToken(), []
    t = spawn(sup, clock, lambda j: run(routed(sup, tr, cfg(p), journal=j), "k", "c", token),
              out)
    until(lambda: p.in_flight == 1)
    token.cancel("operator")
    t.join(5)
    assert isinstance(out[0], ProviderCancelled)
    v = tr.view("local")
    assert (v.state, v.suspect, v.request_failures) == (I, None, 0)
    assert ledger(sup.conn, "k") == [(1, "local", "lost")]
    sup.close()


# ---------------------------------------------------------------------- replacement
def test_replacement_drains_the_active_call_and_serves_new_arrivals(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    seed(sup.inference, "c2")
    gate = threading.Event()
    old = fake(scripts={"k1": Script(delays=(0.0, 1.0))}, sleep=lambda _s, _t: gate.wait(5))
    new = fake()
    tr, _ = tracker(TrackerPolicy(replace_wait_s=10.0))
    a_out, c_out, rep = [], [], []
    a = spawn(sup, clock, lambda j: run(routed(sup, tr, cfg(old), journal=j), "k1"), a_out)
    until(lambda: old.in_flight == 1)
    rt = thread_run(lambda: tr.replace(cfg(new), drain_timeout_s=10.0), rep)
    until(lambda: tr.state("local") is UNLOADING)
    c = spawn(sup, clock, lambda j: run(routed(sup, tr, cfg(old), journal=j), "k2", "c2"),
              c_out)
    c.join(0.1)
    assert c.is_alive() and not c_out                         # waits for the successor
    gate.set()
    for th in (a, rt, c):
        th.join(10)
    assert a_out[0].provider == "local" and old.count() == 1
    assert c_out[0].committed.content and new.count() == 1
    assert (rep[0].generation, rep[0].drained, rep[0].cancelled) == (2, 1, 0)
    assert [call_row(sup.conn, k)["instance_ref"] for k in ("k1", "k2")] == [
        "local:1.1", "local:2.1"]
    assert tr.path("local", 1) == [D, LO, R, E, UNLOADING, UNLOADED]
    assert tr.path("local", 2) == [D, LO, R, E, I]
    assert tr.scheduler.stats()["local"].in_flight == 0
    sup.close()


def test_drain_deadline_cancels_the_old_call_and_reruns_it_on_the_successor(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    old = fake(scripts={"k": Script(delays=(0.0, 30.0))})     # stalls on its own token
    new = fake()
    tr, _ = tracker()
    out: list = []
    t = spawn(sup, clock, lambda j: run(routed(sup, tr, cfg(old), journal=j), "k"), out)
    until(lambda: old.in_flight == 1)
    report = tr.replace(cfg(new), drain_timeout_s=0.05)
    t.join(10)
    assert (report.drained, report.cancelled, report.abandoned) == (0, 1, 0)
    assert out[0].committed.content and new.count("k") == 1
    assert ledger(sup.conn, "k") == [(1, "local", "lost"), (2, "local", "settled")]
    assert answers(sup.conn, "k") == 1 and call_row(sup.conn, "k")["instance_ref"] == "local:2.1"
    assert old.in_flight == 0 and tr.scheduler.stats()["local"].in_flight == 0
    sup.close()


def test_arrival_during_replacement_without_wait_fails_over_without_an_attempt(tmp_path,
                                                                                clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    seed(sup.inference, "c2")
    gate = threading.Event()
    a = fake("a", scripts={"k1": Script(delays=(0.0, 1.0))}, sleep=lambda _s, _t: gate.wait(5))
    b = fake("b")
    tr, _ = tracker(TrackerPolicy(replace_wait_s=0.0))
    out, rep = [], []
    t = spawn(sup, clock, lambda j: run(routed(sup, tr, cfg(a), cfg(b), journal=j), "k1"), out)
    until(lambda: a.in_flight == 1)
    rt = thread_run(lambda: tr.replace(cfg(fake("a")), drain_timeout_s=10.0), rep)
    until(lambda: tr.state("a") is UNLOADING)
    res = run(routed(sup, tr, cfg(a), cfg(b)), "k2", "c2")   # a is draining: no wait budget
    assert (res.provider, res.degraded) == ("b", True)
    assert ledger(sup.conn, "k2") == [(1, "b", "settled")]
    gate.set()
    t.join(10)
    rt.join(10)
    assert out[0].provider == "a" and rep[0].drained == 1
    sup.close()


def test_replacement_mid_load_is_judged_again_against_the_new_window(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    gate = threading.Event()
    old = fake(on_health=lambda _t: gate.wait(5))
    tiny = fake(window=20)
    tr, _ = tracker()
    out: list = []
    t = spawn(sup, clock, lambda j: run(routed(sup, tr, cfg(old), journal=j), "k"), out)
    until(lambda: old.health_checks == 1)                      # first demand is loading
    report = tr.replace(cfg(tiny), drain_timeout_s=1.0)
    t.join(10)
    gate.set()
    assert report.drained == 0 and isinstance(out[0], ContextTooLong)
    assert tiny.count() == 0 and call_row(sup.conn, "k") is None
    assert tr.path("local", 1) == [D, LO, UNLOADING, UNLOADED]
    sup.close()


def test_replacement_that_turns_cloud_still_needs_the_egress_grant(tmp_path, clock):
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    seed(sup.inference, "c2", Privacy.CLOUD_OK)
    tr, _ = tracker()
    r = routed(sup, tr, cfg(fake("x")))
    remote = fake("x", locality=Locality.CLOUD, price=1)
    tr.replace(cfg(remote), drain_timeout_s=1.0)
    with pytest.raises(EgressDenied):
        run(r, "k1")                                            # local only conversation
    with pytest.raises(EgressDenied):
        run(r, "k2", "c2")                                      # cloud ok, grant absent
    grant(sup, CLOUD_GRANT)
    assert run(r, "k2", "c2").provider == "x" and remote.count() == 1
    sup.close()


# ---------------------------------------------------------------------- identity and restart
def test_replacement_keeps_identity_and_conversation_and_resends_full_context(tmp_path,
                                                                              clock):
    sup = boot(tmp_path, clock)
    uuid = sup.identity.uuid
    seed(sup.inference)
    old, new = fake(), fake()
    tr, _ = tracker()
    r = routed(sup, tr, cfg(old))
    first = run(r, "k1").committed.content
    before = {t: rows(sup.conn, t) for t in ("conversations", "messages", "inference_calls")}
    tr.replace(cfg(new), drain_timeout_s=1.0)
    assert {t: rows(sup.conn, t) for t in before} == before   # the tracker writes nothing
    sup.inference.append("c", Role.USER, "and then", 3)
    run(r, "k2")
    sent = new.invocations[0].messages
    assert [m.role for m in sent] == [Role.SYSTEM, Role.USER, Role.ASSISTANT, Role.USER]
    assert sent[2].content == first and sup.identity.uuid == uuid
    sup.close()


def test_restart_rebuilds_lifecycle_and_replays_without_provider_calls(tmp_path, clock):
    sup = boot(tmp_path, clock)
    uuid = sup.identity.uuid
    seed(sup.inference)
    tr, _ = tracker()
    first = run(routed(sup, tr, cfg(fake())), "k1").committed.content
    assert tr.state("local") is I
    sup.close()

    again = boot(tmp_path, clock)                               # new process: nothing kept
    p = fake()
    tr2, _ = tracker()
    r = routed(again, tr2, cfg(p))
    assert tr2.state("local") is D and again.identity.uuid == uuid
    replay = run(r, "k1")
    assert replay.committed.replayed and replay.committed.content == first
    assert (p.count(), p.health_checks, tr2.state("local")) == (0, 0, D)
    again.inference.append("c", Role.USER, "next", 3)
    run(r, "k2")
    assert [m.role for m in p.invocations[0].messages] == [
        Role.SYSTEM, Role.USER, Role.ASSISTANT, Role.USER]
    assert tr2.path("local") == [D, LO, R, E, I]
    again.close()


# ---------------------------------------------------------------------- soak
def test_concurrent_calls_faults_and_replacements_leak_nothing(tmp_path, clock):
    rng = random.Random(5)
    sup = boot(tmp_path, clock)
    faults = [None, None, lambda: ModelCrashed("x"), lambda: StreamInterrupted("x"),
              lambda: Timeout("x"), lambda: InvalidRequest("x")]
    n_threads, per = 4, 50
    scripts = {}
    for i in range(n_threads * per):
        seed(sup.inference, f"c{i}")
        f = rng.choice(faults)
        if f is not None:
            scripts[(f"k{i}", 1)] = Script(fault=Fault(rng.randrange(3), f))  # first try
    names = ("a", "b", "c")
    configs = [cfg(fake(n, slots=2, scripts=scripts)) for n in names]
    tr, _ = tracker(TrackerPolicy(replace_wait_s=0.5))
    out: list = []
    threads = []
    for w in range(n_threads):
        def work(j, w=w) -> list:
            r = routed(sup, tr, *configs, journal=j, max_queue_wait_s=1.0)
            res = []
            for i in range(w * per, (w + 1) * per):
                try:
                    res.append(run(r, f"k{i}", f"c{i}").provider)
                except ProviderError as exc:
                    res.append(type(exc).__name__)
            return res

        threads.append(spawn(sup, clock, work, out))
    def dispatched() -> int:
        return sum(c.provider.count() for c in configs)

    for k, n in enumerate(("a", "b")):
        until(lambda k=k: dispatched() > 40 * (k + 1))
        tr.replace(cfg(fake(n, slots=2, scripts=scripts)), drain_timeout_s=0.5)
    for t in threads:
        t.join(60)
    assert len(out) == n_threads and all(isinstance(o, list) for o in out)
    outcomes = [x for o in out for x in o]
    assert sum(x in names for x in outcomes) > 150            # fallback rescued most faults
    assert set(outcomes) - set(names) <= {"InvalidRequest", "ModelUnavailable",
                                          "ModelReplaced", "Overloaded"}
    assert all(v.in_flight == 0 and v.queued == 0 for v in tr.scheduler.stats().values())
    for n in names:
        v = tr.view(n)
        assert v.in_flight == 0 and v.state is not ModelState.LOADING
    assert sup.conn.execute("SELECT COUNT(*) FROM messages WHERE call_id IS NOT NULL "
                            "GROUP BY call_id HAVING COUNT(*) > 1").fetchall() == []
    assert tr.view("a").generation == 2 and tr.view("b").generation == 2
    sup.close()
