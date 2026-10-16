"""Live inference (Phase 4.1): Stark's companion replies come from an OpenAI compatible
server through the existing router and executor, and reach the Mac socket and a paired
phone as the same durable rows.

The model is the loopback SSE fixture: a real HTTP server speaking the chat completions
stream, so every byte crosses OpenAICompatProvider exactly as it would from SGLang. These
tests prove the pipeline, not a model; genuine model validation runs separately against a
real server (tools/acceptance.py --inference-url).
"""

from __future__ import annotations

import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark import devices as dv
from gameshark.gateway_client import GatewayError, pair_device
from gameshark.inference import CallSpec, OpenAICompatProvider, ProviderConfig
from gameshark.inference.tracker import LoadPlan, TrackerPolicy
from gameshark.stark_chat import CONVERSATION
from gameshark.store import connect
from tests.gateway_runtime import Runtime, short_dir
from tests.helpers import AGENT
from tests.openai_fixture import OpenAIFixture, Reply, completion

NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}}
QUICK = TrackerPolicy(plans=(LoadPlan("probe", probe_timeout_s=0.3),), cooldown_s=0.3)


# ---------------------------------------------------------------------- helpers
def http_provider(base: str, **kw):
    def make() -> list[ProviderConfig]:
        p = OpenAICompatProvider("local", f"{base}/v1", "m", 4096, max_concurrency=2,
                                 extra_body=NO_THINK, read_idle_s=5.0, **kw)
        return [ProviderConfig(p, fingerprint=p.config_fingerprint)]
    return make


def runtime(tmp_path, base: str, *, lan: bool = False, spec: CallSpec | None = None,
            sock_dir: Path | None = None, tracker: TrackerPolicy = QUICK) -> Runtime:
    return Runtime(tmp_path / "runtime.db", sock_dir or short_dir(), lan=lan,
                   providers=http_provider(base), spec=spec, tracker_policy=tracker).start()


def rows(db: Path) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT seq, role, content FROM messages WHERE conversation_id=? "
                            "ORDER BY seq", (CONVERSATION,)).fetchall()
    finally:
        conn.close()


def roles(db: Path) -> list[str]:
    return [r[1] for r in rows(db)]


def calls(db: Path) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT call_id, status, attempts FROM inference_calls "
                            "ORDER BY created").fetchall()
    finally:
        conn.close()


def wait_for(pred, timeout: float = 15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if (v := pred()):
            return v
        time.sleep(0.02)
    raise AssertionError("condition never held")


def gen(c) -> int:
    return c.call("snapshot")["presence"]["generation"]


def send(c, mid: str, text: str, g: int | None = None) -> dict:
    return c.call("send", conversation_id=CONVERSATION, client_msg_id=mid, text=text,
                  presence_gen=gen(c) if g is None else g)


def reply_state(c) -> dict | None:
    return c.call("snapshot")["activity"].get("reply")


def assistant_rows(c) -> list[dict]:
    """Every assistant row pushed to this session so far, in arrival order."""
    out = []
    for e in list(c.events):
        if e["kind"] == "messages":
            out += [r for r in e["data"]["rows"] if r["role"] == "assistant"]
    return out


def pump_events(c, until, timeout: float = 15.0) -> None:
    """Read pushed frames into c.events until until(c) holds."""
    end = time.monotonic() + timeout
    while not until(c):
        if time.monotonic() > end:
            raise AssertionError("pushed state never arrived")
        c.call("ping")                    # any events queued ahead of the pong are kept


def paired(rt: Runtime, name: str = "Jae's iPhone"):
    conn = connect(rt.db)
    try:
        pid, secret = dv.create_pairing(conn, AGENT, name, time.time(), 300.0)
    finally:
        conn.close()
    return pair_device("127.0.0.1", rt.port, rt.identity.pin, pid, secret, name)


def jack_in(mac, phone, hid: str) -> int:
    g = gen(phone)
    assert phone.call("jack", handoff_id=hid, to="self", presence_gen=g)["state"] == "preparing"
    out = mac.call("ready_out", handoff_id=hid, presence_gen=g, draft=None)
    phone.call("ack_in", handoff_id=hid, presence_gen=out["to_gen"])
    return out["to_gen"]


def jack_home(mac, phone, hid: str) -> int:
    g = gen(phone)
    assert phone.call("jack", handoff_id=hid, to="home", presence_gen=g)["state"] == "preparing"
    out = phone.call("ready_out", handoff_id=hid, presence_gen=g, draft=None)
    mac.call("ack_in", handoff_id=hid, presence_gen=out["to_gen"])
    return out["to_gen"]


def gated(fx: OpenAIFixture, text: list[str]) -> threading.Event:
    """Hold every chat request until the returned event is set, then answer text."""
    gate = threading.Event()

    def route(_body):
        gate.wait(20)
        return Reply(parts=[completion(text)])
    fx.routes["/v1/chat/completions"] = route
    return gate


def refused_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def fx():
    f = OpenAIFixture()
    yield f
    f.stop()


# ---------------------------------------------------------------------- delivery
def test_a_provider_reply_reaches_the_mac_and_the_phone_as_one_row(tmp_path, fx):
    fx.routes["/v1/chat/completions"] = Reply(parts=[completion(["Rayleigh", " scattering."])])
    rt = runtime(tmp_path, fx.base, lan=True)
    try:
        device_id, _surface, key = paired(rt)
        with rt.client() as mac, rt.device(device_id, key) as phone:
            for c in (mac, phone):
                c.call("sync", conversation_id=CONVERSATION, after_seq=-1)
            model = mac.call("snapshot")["model"]
            assert model == {"provider": "local", "model": "m", "locality": "local",
                             "fake": False}
            assert send(mac, "m1", "Why is the sky blue?")["seq"] == 1
            for c in (mac, phone):
                pump_events(c, lambda s: assistant_rows(s))
            assert assistant_rows(mac) == assistant_rows(phone) == [
                {"seq": 2, "role": "assistant", "content": "Rayleigh scattering."}]
            assert reply_state(mac) is None
        body = fx.chats()[0]
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        assert body["model"] == "m" and body["stream"] is True
        assert [m["role"] for m in body["messages"]] == ["system", "user"]
    finally:
        rt.close()


def test_a_follow_up_from_the_phone_carries_the_mac_turn_as_context(tmp_path, fx):
    def route(body):
        n = sum(m["role"] == "user" for m in body["messages"])
        return Reply(parts=[completion([f"answer {n}"])])
    fx.routes["/v1/chat/completions"] = route
    rt = runtime(tmp_path, fx.base, lan=True)
    try:
        device_id, _surface, key = paired(rt)
        with rt.client() as mac, rt.device(device_id, key) as phone:
            send(mac, "m1", "Name a prime.")
            wait_for(lambda: roles(rt.db)[-1] == "assistant")
            g = jack_in(mac, phone, "in1")
            send(phone, "p1", "Now double it.", g)
            wait_for(lambda: len(rows(rt.db)) == 5)
        second = fx.chats()[1]["messages"]
        assert [(m["role"], m["content"]) for m in second[1:]] == [
            ("user", "Name a prime."), ("assistant", "answer 1"), ("user", "Now double it.")]
        assert second[0]["role"] == "system"
        assert rows(rt.db)[-1][1:] == ("assistant", "answer 2")
    finally:
        rt.close()


def test_a_long_conversation_keeps_the_system_row_and_the_newest_turns(tmp_path, fx):
    fx.routes["/v1/chat/completions"] = Reply(parts=[completion(["ok"])])
    spec = CallSpec(max_tokens=64, timeout_s=10.0, history_budget=400)
    rt = runtime(tmp_path, fx.base, spec=spec)
    try:
        with rt.client() as mac:
            for i in range(6):
                send(mac, f"m{i}", f"turn {i} " + "x" * 60)
                wait_for(lambda i=i: len(rows(rt.db)) == 3 + 2 * i)
        last = fx.chats()[-1]["messages"]
        assert last[0]["role"] == "system"
        assert last[-1]["content"].startswith("turn 5 ")
        assert len(last) < 12                             # older turns left the window
        assert not any(m["content"].startswith("turn 0 ") for m in last)
        size = sum(len(m["content"].encode()) + 8 for m in last) + 16
        assert size <= 400
    finally:
        rt.close()


# ---------------------------------------------------------------------- duplicates
def test_a_resent_message_id_makes_one_request_and_one_reply(tmp_path, fx):
    gate = gated(fx, ["once"])
    sock_dir = short_dir()
    rt = runtime(tmp_path, fx.base, sock_dir=sock_dir)
    try:
        with rt.client() as mac:
            g = gen(mac)
            first = send(mac, "m1", "count me once", g)
            wait_for(lambda: len(fx.chats()) == 1)
            again = send(mac, "m1", "count me once", g)  # resent while the reply runs
            assert (first["replayed"], again["replayed"], again["seq"]) == (False, True, 1)
            gate.set()
            wait_for(lambda: roles(rt.db)[-1] == "assistant")
    finally:
        rt.close()
    rt = runtime(tmp_path, fx.base, sock_dir=sock_dir)
    try:
        with rt.client() as mac:
            again = send(mac, "m1", "count me once", g)  # the phone's retry after a restart
            assert again == {"conversation_id": CONVERSATION, "seq": 1, "replayed": True}
            time.sleep(0.5)
    finally:
        rt.close()
    assert roles(rt.db) == ["system", "user", "assistant"]
    assert [c[:2] for c in calls(rt.db)] == [(f"call:{CONVERSATION}:1", "done")]
    assert len(fx.chats()) == 1


# ---------------------------------------------------------------------- failure
def test_a_timeout_fails_visibly_and_a_retry_answers_once(tmp_path, fx):
    fx.routes["/v1/chat/completions"] = Reply(parts=[], stall_after=0)
    spec = CallSpec(max_tokens=64, timeout_s=0.3)
    sock_dir = short_dir()
    rt = runtime(tmp_path, fx.base, spec=spec, sock_dir=sock_dir)
    try:
        with rt.client() as mac:
            send(mac, "m1", "are you there?")
            failed = wait_for(lambda: (r := reply_state(mac)) and r["state"] == "failed" and r)
            assert failed["seq"] == 1 and failed["error"] == "AttemptsExhausted"
            snap = mac.call("snapshot")
            assert snap["activity"]["state"] == "blocked"
            assert roles(rt.db) == ["system", "user"]   # nothing fabricated
            assert len(fx.chats()) == 3
    finally:
        rt.close()
    fx.routes["/v1/chat/completions"] = Reply(parts=[completion(["here now"])])
    rt = runtime(tmp_path, fx.base, spec=spec, sock_dir=sock_dir)
    try:
        with rt.client() as mac:
            # The failure is durable: a restarted runtime still reports it.
            assert reply_state(mac)["state"] == "failed"
            g = gen(mac)
            with pytest.raises(GatewayError) as e:
                mac.call("retry_reply", conversation_id=CONVERSATION, seq=1, presence_gen=g + 1)
            assert e.value.code == "stale_presence"
            out = mac.call("retry_reply", conversation_id=CONVERSATION, seq=1, presence_gen=g)
            assert out == {"conversation_id": CONVERSATION, "seq": 1, "retry": 1,
                           "replayed": False}
            dup = mac.call("retry_reply", conversation_id=CONVERSATION, seq=1, presence_gen=g)
            assert (dup["retry"], dup["replayed"]) == (1, True)
            wait_for(lambda: roles(rt.db)[-1] == "assistant")
            time.sleep(0.3)
            # A resend after the answer landed is still the same retry, never a new call.
            late = mac.call("retry_reply", conversation_id=CONVERSATION, seq=1, presence_gen=g)
            assert (late["retry"], late["replayed"]) == (1, True)
            for seq in (0, 2, 9):                     # nothing failed there
                with pytest.raises(GatewayError) as e:
                    mac.call("retry_reply", conversation_id=CONVERSATION, seq=seq,
                             presence_gen=g)
                assert e.value.code == "invalid"
            assert reply_state(mac) is None
            resp = {r["id"]: r["state"] for r in mac.call("snapshot")["responsibilities"]}
            assert resp["chat:stark:main"] == "done"
    finally:
        rt.close()
    assert roles(rt.db) == ["system", "user", "assistant"]
    assert rows(rt.db)[-1][2] == "here now"
    assert [c[:2] for c in calls(rt.db)] == [(f"call:{CONVERSATION}:1", "failed"),
                                             (f"call:{CONVERSATION}:1:r1", "done")]
    assert len(fx.chats()) == 4


def test_a_down_model_shows_waiting_and_answers_once_it_returns(tmp_path):
    port = refused_port()
    rt = runtime(tmp_path, f"http://127.0.0.1:{port}")
    fx = None
    try:
        with rt.client() as mac:
            send(mac, "m1", "hello?")
            waiting = wait_for(lambda: (r := reply_state(mac)) and r["state"] == "waiting"
                               and r)
            assert waiting["seq"] == 1 and waiting["error"] == "ModelUnavailable"
            assert roles(rt.db) == ["system", "user"] and calls(rt.db) == []
            fx = OpenAIFixture(port=port)
            fx.routes["/v1/chat/completions"] = Reply(parts=[completion(["back"])])
            wait_for(lambda: roles(rt.db)[-1] == "assistant", timeout=20)
            time.sleep(0.5)
            assert reply_state(mac) is None
    finally:
        rt.close()
        if fx is not None:
            fx.stop()
    assert roles(rt.db) == ["system", "user", "assistant"]
    assert len(fx.chats()) == 1


# ---------------------------------------------------------------------- transport and presence
def test_a_phone_that_drops_mid_reply_gets_it_once_after_reconnecting(tmp_path, fx):
    gate = gated(fx, ["delivered"])
    rt = runtime(tmp_path, fx.base, lan=True)
    try:
        device_id, _surface, key = paired(rt)
        with rt.client() as mac:
            phone = rt.device(device_id, key)
            phone.call("sync", conversation_id=CONVERSATION, after_seq=-1)
            g = jack_in(mac, phone, "in1")
            send(phone, "p1", "tell me something", g)
            wait_for(lambda: len(fx.chats()) == 1)
            assert wait_for(lambda: reply_state(phone))["state"] == "thinking"
            phone.close()                              # airplane mode mid reply
            gate.set()
            wait_for(lambda: roles(rt.db)[-1] == "assistant")
            with rt.device(device_id, key) as again:
                page = again.call("sync", conversation_id=CONVERSATION, after_seq=1)
                assert [(r["seq"], r["role"], r["content"]) for r in page["rows"]] == [
                    (2, "assistant", "delivered")]
                time.sleep(0.3)
                again.call("ping")
                assert assistant_rows(again) == []     # no second copy pushed after sync
    finally:
        rt.close()
    assert roles(rt.db) == ["system", "user", "assistant"] and len(fx.chats()) == 1


def test_jack_in_and_out_while_a_reply_is_pending(tmp_path, fx):
    gate = gated(fx, ["still here"])
    rt = runtime(tmp_path, fx.base, lan=True)
    try:
        device_id, _surface, key = paired(rt)
        with rt.client() as mac, rt.device(device_id, key) as phone:
            for c in (mac, phone):
                c.call("sync", conversation_id=CONVERSATION, after_seq=-1)
            g0 = gen(mac)
            send(mac, "m1", "think hard", g0)
            wait_for(lambda: len(fx.chats()) == 1)
            g1 = jack_in(mac, phone, "in1")
            assert reply_state(phone)["state"] == "thinking"
            g2 = jack_home(mac, phone, "out1")
            assert g0 < g1 < g2
            gate.set()
            for c in (mac, phone):
                pump_events(c, lambda s: assistant_rows(s))
                assert assistant_rows(c) == [
                    {"seq": 2, "role": "assistant", "content": "still here"}]
            for stale, c in ((g0, mac), (g1, phone)):
                with pytest.raises(GatewayError) as e:
                    send(c, f"stale{stale}", "late", stale)
                assert e.value.code == "stale_presence"
            send(mac, "m2", "back home", g2)
            wait_for(lambda: len(rows(rt.db)) == 5)
    finally:
        rt.close()
    assert roles(rt.db) == ["system", "user", "assistant", "user", "assistant"]
    assert len(fx.chats()) == 2


def test_a_message_the_model_cannot_hold_fails_before_any_request(tmp_path, fx):
    """Codex P2: routing sizes the prompt with the same byte bound as the history window,
    so an oversized message without spaces fails as ContextTooLong locally, never as an
    InvalidRequest from an undersized server."""
    import stark

    args = stark.build_parser().parse_args([
        "run", "--inference-url", f"{fx.base}/v1", "--model", "m",
        "--context-window", "1024", "--max-tokens", "512"])
    rt = Runtime(tmp_path / "runtime.db", short_dir(),
                 providers=lambda: [stark.make_provider(args)], spec=stark.chat_spec(args),
                 tracker_policy=QUICK).start()
    try:
        with rt.client() as mac:
            send(mac, "big", "x" * 8000)
            failed = wait_for(lambda: (r := reply_state(mac)) and r["state"] == "failed" and r)
            assert failed["error"] == "ContextTooLong"
    finally:
        rt.close()
    assert roles(rt.db) == ["system", "user"] and fx.chats() == []
