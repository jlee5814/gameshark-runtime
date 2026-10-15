"""OpenAICompatProvider against a loopback server speaking the chat completions SSE format.

No network endpoint, key, or model weight leaves the machine. Covers SSE framing under
fragmented reads, usage accounting (reported, missing, malformed), stream failures, HTTP
status mapping, cancellation and deadlines during blocked reads, locality and HTTPS
rules, key redaction, router failover, and E1 to E4 recovery with the HTTP provider.
"""

from __future__ import annotations

import http.client
import logging
import re
import sys
import threading
import time
from email.utils import formatdate
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.clock import FakeClock
from gameshark.errors import CrashInjected
from gameshark.inference import (
    AuthFailed,
    BoundKind,
    CallSpec,
    Capability,
    CapabilityUnsupported,
    ContextTooLong,
    EgressDenied,
    FinishReason,
    Health,
    HealthStatus,
    InvalidRequest,
    Locality,
    Message,
    Privacy,
    Provider,
    ProviderCall,
    ProviderCancelled,
    ProviderConfig,
    ProviderRequest,
    ProviderUnavailable,
    RateLimited,
    Role,
    StreamInterrupted,
    StreamNotFinished,
    Timeout,
    TokenBound,
    Usage,
    complete,
)
from gameshark.inference.openai_compat import (
    REDACTED,
    Endpoint,
    OpenAICompatProvider,
    SSEDecoder,
)
from gameshark.lifecycle import CancelToken
from tests.inference_adapter import ask_event
from tests.openai_fixture import (
    OpenAIFixture,
    Reply,
    chunk,
    completion,
    refused_port,
    sse,
    usage_chunk,
)
from tests.test_inference_journal import EXPECT, assert_recovered
from tests.test_inference_journal import boot as boot_assistant
from tests.test_inference_router import (
    CLOUD_GRANT,
    boot,
    call_row,
    grant,
    ledger,
    router,
    seed,
)

KEY = "sk-test-0123456789abcdefSECRET"
TRUSTED = TokenBound(BoundKind.TRUSTED_BYTES)
SPEC = CallSpec(max_tokens=16)
CRASH_POINTS = list(EXPECT)


@pytest.fixture
def srv():
    s = OpenAIFixture()
    yield s
    s.stop()


def local(srv: OpenAIFixture, name: str = "local", **kw) -> OpenAICompatProvider:
    return OpenAICompatProvider(name, f"{srv.base}/v1", "m", 4096, **kw)


def cloud(srv: OpenAIFixture, name: str = "cloud", **kw) -> OpenAICompatProvider:
    """A cloud endpoint by URL; the transport seam routes it to the loopback fixture."""
    kw.setdefault("api_key", KEY)
    return OpenAICompatProvider(
        name, "https://api.cloud.test/v1", "m", 8192, price_in_micros=1000,
        price_out_micros=2000, max_concurrency=4,
        _connect=lambda ep, t: http.client.HTTPConnection("127.0.0.1", srv.port, timeout=t),
        **kw)


def request(call_id: str = "k", text: str = "what is the plan", timeout_s: float = 10.0,
            privacy: Privacy = Privacy.LOCAL_ONLY, **kw) -> ProviderRequest:
    return ProviderRequest(call_id=call_id, attempt=1,
                           messages=(Message(Role.SYSTEM, "be brief"), Message(Role.USER, text)),
                           max_tokens=16, deadline=time.monotonic() + timeout_s,
                           privacy=privacy, **kw)


def run(provider, req: ProviderRequest | None = None, cancel: CancelToken | None = None):
    return complete(provider.generate(req or request(), cancel or CancelToken()))


def failure(provider, req: ProviderRequest | None = None) -> Exception:
    call = provider.generate(req or request(), CancelToken())
    with pytest.raises(Exception) as info:
        complete(call)
    with pytest.raises(type(info.value)):
        call.result()                     # the call keeps its own terminal error
    return info.value


def chat(srv: OpenAIFixture, reply: Reply | list[Reply]) -> None:
    srv.routes["/v1/chat/completions"] = reply


# ---------------------------------------------------------------------- SSE decoder
MIXED = ("retry: 1000\r\n: keepalive\r\n\r\n"
         'data: {"a": "안녕 🙂",\ndata:  "b": 1}\r\n\r\n'
         "event: ping\rid: 7\rdata:x\r\r"
         ": comment\n\n"
         "data: [DONE]\n\n").encode()
MIXED_EVENTS = ['{"a": "안녕 🙂",\n "b": 1}', "x", "[DONE]"]


def test_sse_decoder_whole_stream():
    d = SSEDecoder()
    assert d.feed(MIXED) == MIXED_EVENTS
    d.finish()


def test_sse_decoder_every_split_point_and_bytewise():
    for cut in range(1, len(MIXED)):
        d = SSEDecoder()
        assert d.feed(MIXED[:cut]) + d.feed(MIXED[cut:]) == MIXED_EVENTS, cut
    d = SSEDecoder()
    assert [e for i in range(len(MIXED)) for e in d.feed(MIXED[i:i + 1])] == MIXED_EVENTS


def test_sse_decoder_rejects_bad_utf8_truncated_sequence_and_oversize():
    with pytest.raises(StreamInterrupted, match="UTF 8"):
        SSEDecoder().feed(b"data: \xff\n\n")
    d = SSEDecoder()
    d.feed("data: 🙂".encode()[:-1])
    with pytest.raises(StreamInterrupted, match="inside a UTF 8"):
        d.finish()
    with pytest.raises(StreamInterrupted, match="line exceeded"):
        SSEDecoder(max_line=16).feed(b"data: " + b"x" * 32)
    with pytest.raises(StreamInterrupted, match="exceeded 8 bytes"):
        SSEDecoder(max_bytes=8).feed(b"data: 123456\n\n")


def test_sse_decoder_drops_event_cut_off_by_eof():
    d = SSEDecoder()
    assert d.feed(b"data: partial\n") == []
    d.finish()


# ---------------------------------------------------------------------- normal streams
def test_normal_completion_with_usage_and_wire_shape(srv):
    p = local(srv)
    call = p.generate(request(), CancelToken())
    assert isinstance(p, Provider) and isinstance(call, ProviderCall)
    assert srv.log == []                                       # generate does no I/O
    with pytest.raises(StreamNotFinished):
        call.result()
    chunks = list(call)
    res = call.result()
    call.close()
    assert [c.text for c in chunks] == ["hello", " world"]
    assert [(c.call_id, c.index) for c in chunks] == [("k", 0), ("k", 1)]
    assert (res.text, res.finish_reason, res.usage) == ("hello world", FinishReason.STOP,
                                                        Usage(5, 3))
    assert (res.model, res.completion_id, res.call_id, res.attempt) == ("m", "cmpl-1", "k", 1)
    assert res.instance_ref == f"local@127.0.0.1:{srv.port}"
    (method, path, headers, body), = srv.log
    assert (method, path) == ("POST", "/v1/chat/completions")
    assert body == {"model": "m", "max_tokens": 16, "temperature": 0.0, "stream": True,
                    "stream_options": {"include_usage": True},
                    "messages": [{"role": "system", "content": "be brief"},
                                 {"role": "user", "content": "what is the plan"}]}
    assert headers["Accept"] == "text/event-stream" and "Authorization" not in headers


def test_chunked_transfer_crlf_and_length_finish(srv):
    chat(srv, Reply(parts=[completion(["a", "b"], finish="length", eol="\r\n")], chunked=True,
                    piece=7))
    res = run(local(srv))
    assert (res.text, res.finish_reason) == ("ab", FinishReason.LENGTH)


def test_fragmented_writes_split_unicode_and_keepalives(srv):
    body = b": keepalive\n\n" + completion(["안녕", "하세요 ", "🙂🚀", " é"])
    for piece, chunked in ((1, False), (2, True), (3, False)):
        chat(srv, Reply(parts=[body], piece=piece, chunked=chunked))
        assert run(local(srv)).text == "안녕하세요 🙂🚀 é"


def test_multiline_data_event_and_role_only_chunk(srv):
    raw = (b'data: {"id": "c9", "model": "served-model",\n'
           b'data: "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}]}'
           b"\n\n")
    chat(srv, Reply(parts=[raw + sse(usage_chunk(2, 1, model="served-model", cid="c9"))]))
    res = run(local(srv))
    assert (res.text, res.model, res.completion_id, res.usage) == ("hi", "served-model", "c9",
                                                                   Usage(2, 1))


def test_two_simultaneous_calls_on_one_provider_keep_their_own_results(srv):
    def echo(body: dict) -> Reply:
        q = body["messages"][-1]["content"]
        return Reply(parts=[completion([f"{q}-{i}" for i in range(5)],
                                       usage=(len(q), 5), cid=f"id-{q}")], gap=0.01, piece=9)

    chat(srv, echo)
    p = local(srv, max_concurrency=2)
    out: dict = {}

    def go(q: str) -> None:
        out[q] = run(p, request(call_id=q, text=q))

    threads = [threading.Thread(target=go, args=(q,)) for q in ("alpha", "bravo-two")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert srv.peak == 2                                        # the streams overlapped
    for q, res in out.items():
        assert res.call_id == q and res.completion_id == f"id-{q}"
        assert res.text == "".join(f"{q}-{i}" for i in range(5))
        assert res.usage == Usage(len(q), 5)


def test_empty_content_with_finish_is_a_valid_empty_answer(srv):
    chat(srv, Reply(parts=[completion([])]))
    res = run(local(srv))
    assert (res.text, res.finish_reason) == ("", FinishReason.STOP)


# ---------------------------------------------------------------------- usage
def test_missing_usage_yields_none_never_an_estimate(srv):
    chat(srv, Reply(parts=[completion(["x"], usage=None)]))
    assert run(local(srv)).usage is None


@pytest.mark.parametrize("bad", [{"prompt_tokens": "5", "completion_tokens": 3},
                                 {"prompt_tokens": -1, "completion_tokens": 3},
                                 {"prompt_tokens": True, "completion_tokens": 3},
                                 {"completion_tokens": 3}, [5, 3]])
def test_malformed_usage_is_treated_as_missing(srv, bad):
    chat(srv, Reply(parts=[sse(chunk("x"), chunk(finish="stop"),
                               {"id": "c", "choices": [], "usage": bad})]))
    assert run(local(srv)).usage is None


def test_stream_usage_off_omits_stream_options(srv):
    chat(srv, Reply(parts=[completion(["x"], usage=None)]))
    run(local(srv, stream_usage=False))
    assert "stream_options" not in srv.chats()[0]


# ---------------------------------------------------------------------- broken streams
@pytest.mark.parametrize("reply, match", [
    (Reply(parts=[completion(["a", "b"], done=False)]), "before \\[DONE\\]"),
    (Reply(parts=[sse(chunk("a"), done=False)]), "before \\[DONE\\]"),
    (Reply(parts=[]), "before \\[DONE\\]"),
    (Reply(parts=[sse(chunk("a"))]), "without a finish reason"),
    (Reply(parts=[b"data: {not json\n\n"]), "undecodable"),
    (Reply(parts=[sse({"id": "x"})]), "no choices"),
    (Reply(parts=[sse(chunk("a"), chunk(finish="content_filter"))]), "content_filter"),
    (Reply(parts=[sse(chunk("a"), done=False)], chunked=True, truncate_chunk=True), "failed"),
    (Reply(headers={"Content-Type": "application/json"}, parts=[b"{}"]), "not an event"),
    (Reply(parts=[b"data: \xff\xfe\n\n"]), "UTF 8"),
])
def test_malformed_or_truncated_streams_interrupt(srv, reply, match):
    chat(srv, reply)
    err = failure(local(srv))
    assert isinstance(err, StreamInterrupted)
    assert re.search(match, str(err))


def test_stream_error_object_and_tool_calls(srv):
    chat(srv, Reply(parts=[sse(chunk("a"), {"error": {"message": "engine died"}})]))
    assert isinstance(failure(local(srv)), ProviderUnavailable)
    tool = chunk()
    tool["choices"][0]["delta"]["tool_calls"] = [{"id": "t"}]
    chat(srv, Reply(parts=[sse(tool)]))
    assert isinstance(failure(local(srv)), InvalidRequest)


def test_premature_disconnect_mid_stream_keeps_no_partial_result(srv):
    chat(srv, Reply(parts=[sse(chunk("partial"), done=False)]))
    call = local(srv).generate(request(), CancelToken())
    seen = []
    with pytest.raises(StreamInterrupted):
        for c in call:
            seen.append(c.text)
    assert seen == ["partial"]
    with pytest.raises(StreamInterrupted):
        call.result()


# ---------------------------------------------------------------------- HTTP status
@pytest.mark.parametrize("status, error", [
    (401, AuthFailed), (403, AuthFailed), (500, ProviderUnavailable),
    (502, ProviderUnavailable), (503, ProviderUnavailable), (408, Timeout),
    (400, InvalidRequest), (404, InvalidRequest),
])
def test_status_mapping(srv, status, error):
    chat(srv, Reply(status, {"Content-Type": "application/json"}, [b'{"error": "nope"}']))
    assert type(failure(local(srv))) is error


@pytest.mark.parametrize("headers, expected", [
    ({"Retry-After": "7"}, 7.0),
    ({"retry-after-ms": "1500"}, 1.5),
    ({}, 5.0),
    ({"Retry-After": "99999"}, 300.0),
])
def test_rate_limit_retry_after(srv, headers, expected):
    chat(srv, Reply(429, headers, [b"slow down"]))
    err = failure(local(srv))
    assert isinstance(err, RateLimited) and err.retry_after == expected


def test_rate_limit_retry_after_http_date(srv):
    chat(srv, Reply(429, {"Retry-After": formatdate(time.time() + 30, usegmt=True)}, []))
    err = failure(local(srv))
    assert isinstance(err, RateLimited) and 25 <= err.retry_after <= 31


def test_redirect_is_refused_not_followed(srv):
    chat(srv, Reply(302, {"Location": f"{srv.base}/v1/elsewhere"}, []))
    err = failure(local(srv))
    assert isinstance(err, ProviderUnavailable) and "redirect refused" in str(err)
    assert [p for (_, p, _, _) in srv.log] == ["/v1/chat/completions"]


def test_connection_refused_is_unavailable():
    p = OpenAICompatProvider("gone", f"http://127.0.0.1:{refused_port()}/v1", "m", 4096)
    assert isinstance(failure(p), ProviderUnavailable)
    assert p.health(0.5).status is HealthStatus.DOWN


# ---------------------------------------------------------------------- cancel, deadline
def _cancel_when_stalled(srv: OpenAIFixture, token: CancelToken, at: dict) -> threading.Thread:
    def go() -> None:
        srv.stalled.wait(5)
        time.sleep(0.1)                                         # the reader is blocked now
        at["t"] = time.monotonic()
        token.cancel("operator stop")

    t = threading.Thread(target=go)
    t.start()
    return t


def test_cancel_during_stalled_socket_read(srv):
    chat(srv, Reply(parts=[sse(chunk("first"), done=False), sse(chunk("never"))],
                    stall_after=1))
    token, at = CancelToken(), {}
    call = local(srv, read_idle_s=30).generate(request(timeout_s=30), token)
    t = _cancel_when_stalled(srv, token, at)
    seen = []
    with pytest.raises(ProviderCancelled, match="operator stop"):
        for c in call:
            seen.append(c.text)
    took = time.monotonic() - at["t"]
    t.join()
    assert seen == ["first"] and took < 0.5
    assert srv.client_closed.wait(2)                            # socket closed promptly
    with pytest.raises(ProviderCancelled):
        call.result()


def test_cancel_while_waiting_for_response_headers(srv):
    chat(srv, Reply(stall_before_headers=True))
    token, at = CancelToken(), {}
    call = local(srv).generate(request(timeout_s=30), token)
    t = _cancel_when_stalled(srv, token, at)
    with pytest.raises(ProviderCancelled):
        list(call)
    assert time.monotonic() - at["t"] < 0.5
    t.join()
    assert srv.client_closed.wait(2)


def test_deadline_expires_during_stalled_read(srv):
    chat(srv, Reply(parts=[sse(chunk("first"), done=False)], stall_after=1))
    start = time.monotonic()
    err = failure(local(srv, read_idle_s=30), request(timeout_s=0.4))
    assert isinstance(err, Timeout)
    assert time.monotonic() - start < 0.9
    assert srv.client_closed.wait(2)


def test_idle_stall_within_deadline_interrupts(srv):
    chat(srv, Reply(parts=[sse(chunk("first"), done=False)], stall_after=1))
    err = failure(local(srv, read_idle_s=0.2), request(timeout_s=30))
    assert isinstance(err, StreamInterrupted) and "idle" in str(err)


def test_cancel_wins_over_expired_deadline_and_nothing_is_sent(srv):
    token = CancelToken()
    token.cancel("drain")
    call = local(srv).generate(request(timeout_s=-1), token)
    with pytest.raises(ProviderCancelled):
        list(call)
    with pytest.raises(Timeout):
        list(local(srv).generate(request(timeout_s=-1), CancelToken()))
    assert srv.log == []


def test_close_abandons_stream_and_closes_socket(srv):
    chat(srv, Reply(parts=[sse(chunk("first"), done=False)], stall_after=1))
    call = local(srv).generate(request(), CancelToken())
    it = iter(call)
    assert next(it).text == "first"
    call.close()
    call.close()                                                # idempotent
    assert srv.client_closed.wait(2)
    with pytest.raises(StreamNotFinished):
        call.result()


# ---------------------------------------------------------------------- generate refusals
def test_generate_refuses_before_dispatch(srv):
    p = local(srv)
    with pytest.raises(CapabilityUnsupported):
        p.generate(request(needs={Capability.TOOLS}), CancelToken())
    with pytest.raises(InvalidRequest):
        p.generate(ProviderRequest("k", 1, (Message(Role.TOOL, "x"),), 16,
                                   time.monotonic() + 5), CancelToken())
    with pytest.raises(ContextTooLong):
        p.generate(request(text="w " * 5000), CancelToken())
    with pytest.raises(EgressDenied):
        cloud(srv).generate(request(privacy=Privacy.LOCAL_ONLY), CancelToken())
    assert srv.log == [] and p.capabilities().features == frozenset()


# ---------------------------------------------------------------------- locality, security
@pytest.mark.parametrize("url, locality", [
    ("http://127.0.0.1:30000/v1", Locality.LOCAL),
    ("http://127.8.9.10:1234/v1", Locality.LOCAL),
    ("http://[::1]:8080/v1", Locality.LOCAL),
    ("http://[::ffff:127.0.0.1]:8080/v1", Locality.LOCAL),
    ("https://127.0.0.1/v1", Locality.LOCAL),
    ("https://api.openai.com/v1", Locality.CLOUD),
    ("https://10.0.0.5:30000/v1", Locality.CLOUD),
    ("https://127.0.0.1.nip.io/v1", Locality.CLOUD),
])
def test_locality_derives_from_validated_endpoint(url, locality):
    assert Endpoint.parse(url).locality is locality
    p = OpenAICompatProvider("p", url, "m", 4096)
    assert p.capabilities().locality is locality


@pytest.mark.parametrize("url, match", [
    ("http://localhost:1234/v1", "localhost"),
    ("http://api.example.com/v1", "requires https"),
    ("http://10.0.0.5:30000/v1", "requires https"),
    ("http://192.168.1.2/v1", "requires https"),
    ("https://user:pw@api.example.com/v1", "credentials"),
    ("ftp://127.0.0.1/v1", "scheme"),
    ("https://api.example.com/v1?key=x", "query"),
])
def test_endpoint_rejections(url, match):
    with pytest.raises(ValueError, match=match):
        Endpoint.parse(url)


def test_locality_label_cannot_be_supplied():
    with pytest.raises(TypeError):
        OpenAICompatProvider("p", "https://api.example.com/v1", "m", 4096,
                             locality=Locality.LOCAL)


def test_key_is_sent_on_the_wire_and_redacted_everywhere_else(srv, caplog):
    caplog.set_level(logging.DEBUG)
    p = cloud(srv)
    assert KEY not in repr(p) and REDACTED in repr(p) and KEY not in p.config_fingerprint
    run(p, request(privacy=Privacy.CLOUD_OK))
    assert srv.log[-1][2]["Authorization"] == f"Bearer {KEY}"

    echo = f'{{"error": "bad key {KEY}", "hint": "Authorization: Bearer {KEY}"}}'.encode()
    chat(srv, Reply(401, {"Content-Type": "application/json"}, [echo]))
    err = failure(p, request(privacy=Privacy.CLOUD_OK))
    assert isinstance(err, AuthFailed) and KEY not in str(err) and REDACTED in str(err)
    srv.routes["/v1/models"] = Reply(500, {}, [echo])
    health = p.health(1.0)
    assert health.status is HealthStatus.DOWN and KEY not in health.detail
    assert KEY not in caplog.text


def test_config_fingerprint_tracks_key_endpoint_and_model(srv):
    a, b = cloud(srv), cloud(srv, api_key=KEY + "x")
    assert a.config_fingerprint == cloud(srv).config_fingerprint != b.config_fingerprint
    assert local(srv).config_fingerprint != a.config_fingerprint


# ---------------------------------------------------------------------- health
def test_health_ok_degraded_down(srv):
    p = local(srv)
    assert p.health(1.0).status is HealthStatus.OK
    srv.routes["/v1/models"] = Reply(headers={}, parts=[b'{"data": [{"id": "other"}]}'])
    assert p.health(1.0).status is HealthStatus.DEGRADED
    srv.routes["/v1/models"] = Reply(401, {}, [b"no"])
    assert p.health(1.0).status is HealthStatus.DOWN
    srv.routes["/v1/models"] = Reply(headers={}, parts=[b"not json"])
    assert p.health(1.0).status is HealthStatus.DOWN


# ---------------------------------------------------------------------- router integration
def all_text(conn) -> str:
    out = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        for row in conn.execute(f"SELECT * FROM {table}"):
            out.extend(str(v) for v in tuple(row))
    return "\n".join(out)


def test_router_commits_http_answer_and_settles_reported_usage(tmp_path, clock, srv):
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    out = router(sup, _cfg(local(srv))).complete("k", "c", SPEC, CancelToken())
    assert (out.committed.content, out.provider, out.metered) == ("hello world", "local", True)
    row = sup.conn.execute("SELECT status, tokens_in, tokens_out FROM budget_ledger").fetchone()
    assert tuple(row) == ("settled", 5, 3)
    sent = srv.chats()[0]["messages"]                           # full context from the journal
    assert [m["role"] for m in sent] == ["system", "user"]
    sup.close()


def _cfg(provider, bound: TokenBound = TRUSTED, **kw) -> ProviderConfig:
    return ProviderConfig(provider, token_bound=bound,
                          fingerprint=getattr(provider, "config_fingerprint", ""), **kw)


def test_missing_usage_through_router_charges_full_reservation(tmp_path, clock, srv):
    chat(srv, Reply(parts=[completion(["x"], usage=None)]))
    sup = boot(tmp_path, clock)
    grant(sup, CLOUD_GRANT)
    seed(sup.inference, "c", Privacy.CLOUD_OK)
    out = router(sup, _cfg(cloud(srv))).complete("k", "c", SPEC, CancelToken())
    assert out.metered is False and out.overrun is None and out.committed.content == "x"
    row = sup.conn.execute(
        "SELECT status, tokens_in, tokens_out, reserved_tokens, reserved_micros, cost_micros "
        "FROM budget_ledger WHERE call_id='k'").fetchone()
    assert row["status"] == "unmetered"
    assert (row["tokens_in"], row["tokens_out"]) == (None, None)        # nothing invented
    assert row["cost_micros"] == row["reserved_micros"] > 0             # charged in full
    spent = sup.inference.spend()
    assert spent == {"tokens": row["reserved_tokens"], "micros": row["reserved_micros"]}
    assert sup.inference.overruns() == []
    sup.close()


def test_failed_stream_commits_no_partial_text_and_falls_back(tmp_path, clock, srv):
    other = OpenAIFixture()
    try:
        chat(srv, Reply(parts=[sse(chunk("half an"), chunk(" answer"), done=False)]))
        chat(other, Reply(parts=[completion(["whole answer"])]))
        sup = boot(tmp_path, clock)
        seed(sup.inference)
        a, b = local(srv, "a"), local(other, "b")
        out = router(sup, _cfg(a), _cfg(b)).complete("k", "c", SPEC, CancelToken())
        assert (out.provider, out.degraded, out.committed.content) == ("b", True,
                                                                       "whole answer")
        texts = [r[0] for r in sup.conn.execute("SELECT content FROM messages")]
        assert not any("half" in t for t in texts)
        assert ledger(sup.conn, "k") == [(1, "a", "lost"), (2, "b", "settled")]
        assert call_row(sup.conn, "k")["degraded"] == 1
        sup.close()
    finally:
        other.stop()


def test_failed_only_provider_leaves_call_pending_with_no_message(tmp_path, clock, srv):
    chat(srv, Reply(parts=[sse(chunk("partial"), done=False)]))
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    with pytest.raises(StreamInterrupted):
        router(sup, _cfg(local(srv))).complete("k", "c", SPEC, CancelToken())
    assert sup.conn.execute("SELECT COUNT(*) FROM messages WHERE call_id='k'").fetchone()[0] == 0
    assert call_row(sup.conn, "k")["status"] == "pending"
    assert ledger(sup.conn, "k") == [(1, "local", "lost")]
    sup.close()


def test_router_falls_back_from_refused_local_provider(tmp_path, clock, srv):
    dead = OpenAICompatProvider("dead", f"http://127.0.0.1:{refused_port()}/v1", "m", 4096)
    dead.health = lambda timeout_s: Health(HealthStatus.OK)    # a probe that predates the crash
    sup = boot(tmp_path, clock)
    seed(sup.inference)
    out = router(sup, _cfg(dead), _cfg(local(srv, "live"))).complete("k", "c", SPEC,
                                                                      CancelToken())
    assert (out.provider, out.degraded) == ("live", True)
    assert ledger(sup.conn, "k") == [(1, "dead", "lost"), (2, "live", "settled")]
    assert call_row(sup.conn, "k")["degraded"] == 1
    sup.close()


def test_cloud_http_provider_needs_durable_grant(tmp_path, clock, srv):
    sup = boot(tmp_path, clock)
    seed(sup.inference, "c", Privacy.CLOUD_OK)
    seed(sup.inference, "private", Privacy.LOCAL_ONLY)
    p = cloud(srv)
    with pytest.raises(EgressDenied):
        router(sup, _cfg(p)).complete("k", "c", SPEC, CancelToken())
    assert srv.chats() == []                                    # zero cloud bytes
    grant(sup, CLOUD_GRANT)
    with pytest.raises(EgressDenied):
        router(sup, _cfg(p)).complete("k2", "private", SPEC, CancelToken())
    assert srv.chats() == []
    out = router(sup, _cfg(p)).complete("k", "c", SPEC, CancelToken())
    assert out.provider == "cloud" and out.committed.content == "hello world"
    assert KEY not in all_text(sup.conn)
    sup.close()


def test_auth_failure_records_fingerprint_not_key(tmp_path, clock, srv):
    chat(srv, Reply(401, {}, [KEY.encode()]))
    sup = boot(tmp_path, clock)
    grant(sup, CLOUD_GRANT)
    seed(sup.inference, "c", Privacy.CLOUD_OK)
    p = cloud(srv)
    with pytest.raises(AuthFailed):
        router(sup, _cfg(p)).complete("k", "c", SPEC, CancelToken())
    assert call_row(sup.conn, "k")["error_kind"] == f"AuthFailed:cloud:{p.config_fingerprint}"
    assert KEY not in all_text(sup.conn)
    sup.close()


def test_rate_limit_from_http_opens_circuit(tmp_path, clock, srv):
    other = OpenAIFixture()
    try:
        chat(srv, Reply(429, {"Retry-After": "30"}, []))
        sup = boot(tmp_path, clock)
        seed(sup.inference, "c1")
        seed(sup.inference, "c2")
        r = router(sup, _cfg(local(srv, "a")), _cfg(local(other, "b")))
        assert r.complete("k1", "c1", SPEC, CancelToken()).provider == "b"
        assert r.complete("k2", "c2", SPEC, CancelToken()).provider == "b"
        assert len(srv.chats()) == 1                            # circuit kept a out
        sup.close()
    finally:
        other.stop()


# ---------------------------------------------------------------------- E1 to E4
@pytest.mark.parametrize("label", CRASH_POINTS)
def test_crash_points_recover_with_http_provider(tmp_path, srv, label):
    old_count, new_counts, expected_ledger = EXPECT[label]
    clock = FakeClock()
    first = boot_assistant(tmp_path, clock, provider=local(srv), crash_at={label})
    uuid = first.identity.uuid
    with pytest.raises(CrashInjected, match=label):
        first.submit(ask_event())
    assert len(srv.chats()) == old_count
    first.close()

    second = boot_assistant(tmp_path, clock, provider=local(srv))   # start() replays
    assert len(srv.chats()) == old_count + sum(new_counts.values())
    assert [tuple(r) for r in second.conn.execute(
        "SELECT attempt, status FROM budget_ledger ORDER BY attempt")] == expected_ledger
    assert_recovered(second, uuid)
    second.close()
