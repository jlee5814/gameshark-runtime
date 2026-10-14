"""Provider contracts and FakeProvider, in process with no I/O.

Completion belongs to the call, never to the provider: interleaved and concurrent calls on
one provider must keep their own text, usage, and errors. Deadlines run on a FakeClock.
"""

from __future__ import annotations

import dataclasses
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.clock import FakeClock
from gameshark.errors import Cancelled
from gameshark.inference import (
    AttemptsExhausted,
    AuthFailed,
    BudgetExceeded,
    Capabilities,
    Capability,
    CapabilityUnsupported,
    ContextConflict,
    ContextTooLong,
    EgressDenied,
    FakeProvider,
    Fault,
    FinishReason,
    Health,
    HealthStatus,
    InvalidRequest,
    Locality,
    Message,
    ModelCrashed,
    Overloaded,
    Provider,
    ProviderCall,
    ProviderCancelled,
    ProviderError,
    ProviderRequest,
    ProviderResult,
    ProviderUnavailable,
    RateLimited,
    Role,
    Script,
    StreamInterrupted,
    StreamNotFinished,
    Timeout,
    Usage,
    complete,
)
from gameshark.lifecycle import CancelToken

FAR = 1e18  # a deadline nothing reaches


def msgs(text: str = "hello there") -> list[Message]:
    return [Message(Role.SYSTEM, "you are stark"), Message(Role.USER, text)]


def request(call_id: str = "c1", attempt: int = 1, deadline: float = FAR, **kw) -> ProviderRequest:
    kw.setdefault("messages", msgs())
    kw.setdefault("max_tokens", 64)
    return ProviderRequest(call_id=call_id, attempt=attempt, deadline=deadline, **kw)


def fake_time(clock: FakeClock) -> dict:
    return {"now": clock.now, "sleep": lambda s, _tok: clock.advance(s)}


# Contracts and immutability


def test_values_are_frozen():
    caps = Capabilities(model="m", locality=Locality.LOCAL, context_window=8)
    req = request()
    res = ProviderResult("c1", 1, "x", FinishReason.STOP, Usage(1, 1), "m")
    for value, attr in ((caps, "context_window"), (req.messages[0], "content"),
                        (req, "call_id"), (res, "text"), (res.usage, "tokens_out")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, attr, None)


def test_request_snapshots_its_context():
    source = msgs()
    req = request(messages=source, needs={"tools"})
    source.append(Message(Role.USER, "smuggled"))
    assert isinstance(req.messages, tuple) and len(req.messages) == 2
    assert req.needs == frozenset({Capability.TOOLS})


@pytest.mark.parametrize("kw", [
    {"call_id": ""}, {"attempt": 0}, {"attempt": 4}, {"messages": []}, {"max_tokens": 0},
    {"messages": [{"role": "user", "content": "raw"}]},
])
def test_request_rejects_invalid_fields(kw):
    with pytest.raises((ValueError, TypeError)):
        request(**kw)


@pytest.mark.parametrize("kw", [
    {"context_window": 0}, {"max_concurrency": 0}, {"price_in_micros": -1}, {"model": ""},
])
def test_capabilities_reject_invalid_fields(kw):
    base = {"model": "m", "locality": Locality.LOCAL, "context_window": 8}
    with pytest.raises(ValueError):
        Capabilities(**{**base, **kw})


def test_request_carries_no_identity():
    names = {f.name for f in dataclasses.fields(ProviderRequest)}
    assert names == {"call_id", "attempt", "messages", "max_tokens", "deadline", "privacy",
                     "temperature", "needs"}


def test_completion_belongs_to_the_call_not_the_provider():
    provider = FakeProvider()
    call = provider.generate(request(), CancelToken())
    assert isinstance(provider, Provider)
    assert isinstance(call, ProviderCall)
    assert not hasattr(Provider, "result") and not hasattr(provider, "result")


# Deterministic streaming


def test_output_is_deterministic_per_request():
    a = complete(FakeProvider().generate(request("c1"), CancelToken()))
    b = complete(FakeProvider().generate(request("c1"), CancelToken()))
    c = complete(FakeProvider().generate(request("c2"), CancelToken()))
    assert (a.text, a.usage) == (b.text, b.usage)
    assert c.text != a.text


def test_stream_completes_with_usage():
    provider = FakeProvider()
    call = provider.generate(request(), CancelToken())
    with pytest.raises(StreamNotFinished):
        call.result()
    chunks = list(call)
    res = call.result()
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert "".join(c.text for c in chunks) == res.text
    assert res.usage == Usage(tokens_in=5, tokens_out=len(chunks))
    assert (res.call_id, res.attempt, res.finish_reason) == ("c1", 1, FinishReason.STOP)
    assert res.model == "fake-model"
    assert provider.in_flight == 0


def test_max_tokens_truncates_with_length_finish():
    res = complete(FakeProvider().generate(request(max_tokens=3), CancelToken()))
    assert res.usage.tokens_out == 3 and res.finish_reason is FinishReason.LENGTH


def test_closed_stream_has_no_result():
    provider = FakeProvider()
    call = provider.generate(request(), CancelToken())
    next(iter(call))
    call.close()
    call.close()
    with pytest.raises(StreamNotFinished):
        call.result()
    assert provider.in_flight == 0


# Isolation between calls on one provider


def test_interleaved_calls_never_cross():
    crash = ModelCrashed("b died")
    provider = FakeProvider(scripts={
        "a": Script(chunks=("a0 ", "a1 ", "a2 ", "a3")),
        "b": Script(chunks=("b0 ", "b1 ", "b2 ", "b3"), fault=Fault(2, lambda: crash)),
    })
    call_a = provider.generate(request("a", messages=msgs("one two three")), CancelToken())
    call_b = provider.generate(request("b", messages=msgs("four")), CancelToken())
    sa, sb = iter(call_a), iter(call_b)

    seen = [next(sa), next(sb), next(sa), next(sb)]
    assert provider.peak_in_flight == 2
    with pytest.raises(ModelCrashed):
        next(sb)
    with pytest.raises(StreamNotFinished):
        call_a.result()                                   # B's failure is not A's ending
    seen += list(sa)

    assert [c.text for c in seen if c.call_id == "a"] == ["a0 ", "a1 ", "a2 ", "a3"]
    assert [c.text for c in seen if c.call_id == "b"] == ["b0 ", "b1 "]
    res_a = call_a.result()
    assert (res_a.call_id, res_a.text) == ("a", "a0 a1 a2 a3")
    assert res_a.usage == Usage(tokens_in=6, tokens_out=4)
    with pytest.raises(ModelCrashed) as err:
        call_b.result()
    assert err.value is crash
    assert provider.in_flight == 0


def test_concurrent_calls_keep_their_own_results():
    n = 8
    barrier = threading.Barrier(n)
    scripts = {f"c{i}": Script(delays=(0.001 * (n - i),) * 3) for i in range(n)}
    scripts["c3"] = Script(fault=Fault(1, lambda: StreamInterrupted("reset")))
    provider = FakeProvider(scripts=scripts)
    outcomes: dict[str, object] = {}

    def run(i: int) -> None:
        req = request(f"c{i}", messages=msgs(" ".join(["w"] * (i + 1))))
        call = provider.generate(req, CancelToken())
        barrier.wait()
        try:
            outcomes[req.call_id] = (req, complete(call))
        except ProviderError as exc:
            outcomes[req.call_id] = (req, exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)

    assert provider.peak_in_flight > 1 and provider.in_flight == 0
    for i in range(n):
        req, outcome = outcomes[f"c{i}"]
        if i == 3:
            assert isinstance(outcome, StreamInterrupted)
            continue
        expected = "".join(FakeProvider().default_text(req))
        assert outcome.call_id == req.call_id
        assert outcome.text == expected
        assert outcome.usage.tokens_in == 3 + i + 1          # "you are stark" plus i + 1 words


# Injected faults and replay counts


@pytest.mark.parametrize("k", [0, 1, 3, 8])
def test_fault_at_chunk_index(k):
    boom = ProviderUnavailable("down")
    provider = FakeProvider(scripts={"c1": Script(fault=Fault(k, lambda: boom))})
    call = provider.generate(request(), CancelToken())
    got = []
    with pytest.raises(ProviderUnavailable) as err:
        got.extend(call)                                  # keeps chunks before the fault
    assert len(got) == k and err.value is boom
    with pytest.raises(ProviderUnavailable) as again:
        call.result()
    assert again.value is boom
    assert provider.in_flight == 0


def test_fault_on_first_attempt_only_and_counts():
    provider = FakeProvider(scripts={
        ("c1", 1): Script(fault=Fault(2, lambda: ModelCrashed("oom"))),
    })
    with pytest.raises(ModelCrashed):
        complete(provider.generate(request("c1", 1), CancelToken()))
    res = complete(provider.generate(request("c1", 2), CancelToken()))
    complete(provider.generate(request("c2"), CancelToken()))
    assert res.attempt == 2 and res.usage.tokens_out == 8
    assert provider.count() == 3
    assert provider.count("c1") == 2
    assert provider.count("c1", attempt=1) == provider.count("c1", attempt=2) == 1


def test_pre_dispatch_refusals_are_not_invocations():
    caps = Capabilities(model="m", locality=Locality.LOCAL, context_window=20,
                        features={Capability.TOOLS})
    provider = FakeProvider(capabilities=caps)
    with pytest.raises(CapabilityUnsupported):
        provider.generate(request(needs={Capability.VISION}), CancelToken())
    with pytest.raises(ContextTooLong):
        provider.generate(request(max_tokens=16), CancelToken())
    provider.generate(request(needs={Capability.TOOLS}, max_tokens=15), CancelToken())
    assert provider.count() == 1


# Cancellation and deadlines


def test_cancel_before_first_chunk():
    provider = FakeProvider()
    token = CancelToken()
    call = provider.generate(request(), token)
    token.cancel("operator stop")
    with pytest.raises(ProviderCancelled, match="operator stop") as err:
        next(iter(call))
    assert isinstance(err.value, Cancelled)
    assert not err.value.retryable
    with pytest.raises(ProviderCancelled):
        call.result()


def test_cancel_wakes_a_long_delay():
    provider = FakeProvider(scripts={"c1": Script(delays=(0.0, 30.0))})
    token = CancelToken()
    call = provider.generate(request(), token)
    stream = iter(call)
    next(stream)
    threading.Timer(0.05, token.cancel, args=("drain deadline reached",)).start()
    started = time.monotonic()
    with pytest.raises(ProviderCancelled):
        next(stream)
    assert time.monotonic() - started < 2.0
    assert provider.in_flight == 0


def test_cancel_beats_expired_deadline(clock):
    provider = FakeProvider(**fake_time(clock))
    token = CancelToken()
    call = provider.generate(request(deadline=clock.now() - 1), token)
    token.cancel("operator stop")
    with pytest.raises(ProviderCancelled):
        complete(call)


def test_expired_deadline_yields_no_chunks(clock):
    provider = FakeProvider(**fake_time(clock))
    call = provider.generate(request(deadline=clock.now()), CancelToken())
    with pytest.raises(Timeout):
        next(iter(call))
    with pytest.raises(Timeout):
        call.result()


def test_delay_crossing_deadline_times_out_mid_stream(clock):
    provider = FakeProvider(scripts={"c1": Script(delays=(1.0, 1.0, 5.0))}, **fake_time(clock))
    call = provider.generate(request(deadline=clock.now() + 4.0), CancelToken())
    got = []
    with pytest.raises(Timeout):
        got.extend(call)
    assert len(got) == 2


def test_cancel_token_wait():
    token = CancelToken()
    assert token.wait(0.01) is False
    token.cancel("x")
    assert token.wait(10) is True


# Error classification and health


@pytest.mark.parametrize("cls,retryable,before_dispatch", [
    (ProviderUnavailable, True, False),
    (ModelCrashed, True, False),
    (Timeout, True, False),
    (RateLimited, True, False),
    (Overloaded, True, True),
    (StreamInterrupted, True, False),
    (ContextConflict, True, True),
    (CapabilityUnsupported, False, True),
    (ContextTooLong, False, True),
    (BudgetExceeded, False, True),
    (EgressDenied, False, True),
    (AuthFailed, False, False),
    (InvalidRequest, False, False),
    (ProviderCancelled, False, False),
    (AttemptsExhausted, False, False),
])
def test_error_classification(cls, retryable, before_dispatch):
    exc = cls("x")
    assert isinstance(exc, ProviderError)
    assert (exc.retryable, exc.before_dispatch) == (retryable, before_dispatch)


def test_errors_carry_retry_hints():
    assert RateLimited("429", retry_after=7.5).retry_after == 7.5
    assert Overloaded("full", retry_after=2.0).retry_after == 2.0
    assert BudgetExceeded("spent", reset_at=123.0).reset_at == 123.0


def test_health_is_scripted_and_counted():
    provider = FakeProvider(health=Health(HealthStatus.DEGRADED, "slow"))
    assert provider.health(1.0) == Health(HealthStatus.DEGRADED, "slow")
    provider.health_status = Health(HealthStatus.DOWN)
    assert provider.health(1.0).status is HealthStatus.DOWN
    assert provider.health_checks == 2
