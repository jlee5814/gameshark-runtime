"""Surface gateway: authentication, framing, the fixed command set, presence fencing, and
resync, against the real run_forever on an in process coordinator thread."""

from __future__ import annotations

import ast
import os
import socket
import sqlite3
import stat
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark import gateway as gw
from gameshark.gateway_client import GatewayClient, GatewayError
from gameshark.stark_chat import CONVERSATION
from gameshark.worker import enqueue_control
from tests.gateway_runtime import Gate, Runtime, short_dir

ROOT = Path(__file__).resolve().parents[1]

# Shared with StarkKitTests.AuthTests: the Swift client must produce the same digest.
KAT_TOKEN = bytes(range(32))
KAT_NONCE = bytes([0xAA]) * 32
KAT_MAC = "9229d616a443a3e82e05a8c81f3b383623e7879c3a33a92a6947729dd42758c9"


@pytest.fixture
def rt(tmp_path):
    r = Runtime(tmp_path / "runtime.db", short_dir()).start()
    yield r
    r.close()


def rows(db: Path) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT seq, role, content FROM messages WHERE conversation_id=? "
                            "ORDER BY seq", (CONVERSATION,)).fetchall()
    finally:
        conn.close()


def gen(c: GatewayClient) -> int:
    return c.call("snapshot")["presence"]["generation"]


def reply(c: GatewayClient) -> dict:
    """The next non event frame. The runtime may push events at any time after auth."""
    while (msg := c.read()).get("type") == "event":
        c.events.append(msg)
    return msg


def push_event(rt) -> None:
    """Queue an unsolicited event ahead of whatever the test sends next."""
    for sess in rt.server.live_sessions():
        sess.send({"type": "event", "kind": "activity", "data": {"working": False}})


def wait_for(pred, timeout: float = 10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if (v := pred()):
            return v
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def raw_connect(rt: Runtime) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(str(rt.sock))
    return s


def read(s: socket.socket) -> dict:
    alive = __import__("threading").Event()
    alive.set()
    return gw.read_frame(s, alive)


# ---------------------------------------------------------------------- auth and framing
def test_hmac_known_answer_matches_swift_vector():
    assert gw.sign(KAT_TOKEN, KAT_NONCE, "mac", 1) == KAT_MAC


def test_socket_and_token_are_private(rt):
    assert (rt.sock.stat().st_mode & 0o777) == 0o600
    assert (rt.sock.parent.stat().st_mode & 0o777) == 0o700
    assert (rt.token_path.stat().st_mode & 0o777) == 0o600


def test_token_readable_by_others_is_refused(tmp_path):
    p = tmp_path / "gateway.token"
    p.write_text("00" * 32)
    os.chmod(p, 0o644)
    with pytest.raises(PermissionError):
        gw.load_or_create_token(p)


def test_peer_uid_is_ours(rt):
    s = raw_connect(rt)
    try:
        assert gw.peer_uid(s) in (os.getuid(), None)
    finally:
        s.close()


def test_wrong_token_is_rejected(rt):
    with pytest.raises(GatewayError) as e:
        GatewayClient(rt.sock, b"\x00" * 32)
    assert e.value.code == "auth_failed"


def test_unsupported_version_is_rejected(rt):
    with pytest.raises(GatewayError) as e:
        GatewayClient(rt.sock, rt.token, proto=99)
    assert e.value.code == "unsupported_version"


def test_invalid_surface_name_is_rejected(rt):
    with pytest.raises(GatewayError) as e:
        GatewayClient(rt.sock, rt.token, surface="../mac")
    assert e.value.code == "invalid"


def test_request_before_hello_ends_session(rt):
    s = raw_connect(rt)
    assert read(s)["type"] == "challenge"
    s.sendall(gw.encode({"type": "send", "id": 1, "text": "sneak"}))
    assert read(s)["code"] == "auth_failed"
    s.close()
    assert [r[1] for r in rows(rt.db)] == ["system"]


def test_oversize_frame_ends_session(rt):
    c = rt.client()
    push_event(rt)
    c.sock.sendall(struct.pack(">I", gw.MAX_FRAME + 1))
    assert reply(c)["code"] == "bad_frame"
    with pytest.raises(ConnectionError):
        reply(c)


def test_malformed_json_ends_session(rt):
    c = rt.client()
    push_event(rt)
    c.sock.sendall(struct.pack(">I", 5) + b"{nope")
    assert reply(c)["code"] == "bad_frame"


# ---------------------------------------------------------------------- socket directory
def test_open_existing_socket_dir_is_refused_and_left_alone():
    d = short_dir()
    os.chmod(d, 0o755)                                    # a shared, caller owned directory
    server = gw.GatewayServer(d / "stark.sock", KAT_TOKEN, {}, threading.Event())
    with pytest.raises(PermissionError):
        server.start()
    assert stat.S_IMODE(d.stat().st_mode) == 0o755
    assert not (d / "stark.sock").exists()


def test_missing_socket_dir_is_created_private_without_touching_its_parent():
    base = short_dir()
    os.chmod(base, 0o755)
    server = gw.GatewayServer(base / "gateway" / "stark.sock", KAT_TOKEN, {},
                              threading.Event())
    server.start()
    try:
        assert stat.S_IMODE((base / "gateway").stat().st_mode) == 0o700
        assert stat.S_IMODE(base.stat().st_mode) == 0o755
    finally:
        server.close()


def test_lan_start_failure_leaves_no_listening_socket():
    """A LAN listener that cannot bind takes the Unix socket down with it: nothing would
    pump an authenticated session, so nothing may accept one."""
    from gameshark.net_gateway import LanListener, load_or_create_identity

    d = short_dir()
    before = set(threading.enumerate())
    busy = socket.create_server(("127.0.0.1", 0))
    try:
        lan = LanListener(load_or_create_identity(d), port=busy.getsockname()[1],
                          host="127.0.0.1")
        server = gw.GatewayServer(d / "stark.sock", KAT_TOKEN, {}, threading.Event())
        server.attach(lan)
        with pytest.raises(OSError):
            server.start()
        assert not server.alive.is_set()
        assert not (d / "stark.sock").exists()
        with pytest.raises(OSError), socket.socket(socket.AF_UNIX) as s:
            s.connect(str(d / "stark.sock"))
        wait_for(lambda: not [t for t in set(threading.enumerate()) - before
                              if t.name in ("gateway-accept", "lan-accept")])
    finally:
        busy.close()


# ---------------------------------------------------------------------- command set
def test_unknown_commands_are_refused_and_change_nothing(rt):
    with rt.client() as c:
        for kind in ("exec", "shell", "run", "eval", "hello", "lost", "__lost__", "welcome"):
            with pytest.raises(GatewayError) as e:
                c.call(kind, cmd="touch /tmp/pwned")
            assert e.value.code == "unknown_command"
        assert c.call("ping")["pong"]
    assert [r[1] for r in rows(rt.db)] == ["system"]


@pytest.mark.parametrize("module", ["gateway.py", "surface_api.py", "net_gateway.py",
                                    "devices.py"])
def test_gateway_modules_cannot_reach_a_shell(module):
    tree = ast.parse((ROOT / "gameshark" / module).read_text())
    imported = {a.name.split(".")[0] for n in ast.walk(tree)
                if isinstance(n, ast.Import | ast.ImportFrom)
                for a in (n.names if isinstance(n, ast.Import) else [ast.alias(n.module or "")])}
    assert not imported & {"subprocess", "pty", "shlex", "multiprocessing", "importlib"}
    calls = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not calls & {"system", "popen", "execv", "execve", "spawn", "eval", "exec",
                        "__import__", "fork"}


def test_send_validates_fields(rt):
    with rt.client() as c:
        g = gen(c)
        bad = [{"client_msg_id": "x" * 200, "text": "hi"},
               {"client_msg_id": "m 1", "text": "hi"},
               {"client_msg_id": "m1", "text": ""},
               {"client_msg_id": "m1", "text": "x" * (9 * 1024)},
               {"client_msg_id": "m1", "text": "hi", "conversation_id": "conv:other"}]
        for fields in bad:
            fields.setdefault("conversation_id", CONVERSATION)
            with pytest.raises(GatewayError) as e:
                c.call("send", presence_gen=g, **fields)
            assert e.value.code == "invalid"
    assert [r[1] for r in rows(rt.db)] == ["system"]


# ---------------------------------------------------------------------- presence fencing
def test_message_persists_and_stark_replies(rt):
    with rt.client() as c:
        c.call("sync", conversation_id=CONVERSATION, after_seq=-1)
        out = c.call("send", conversation_id=CONVERSATION, client_msg_id="m1",
                     text="hello stark", presence_gen=gen(c))
        assert out == {"conversation_id": CONVERSATION, "seq": 1, "replayed": False}
        reply = c.wait_event("messages", lambda d: any(r["role"] == "assistant"
                                                         for r in d["rows"]))
        assert "hello stark" in reply["rows"][-1]["content"]
    assert [r[1] for r in rows(rt.db)] == ["system", "user", "assistant"]


def test_obsolete_presence_generation_writes_nothing(rt):
    with rt.client() as c:
        g = gen(c)
        for stale in (g - 1, g + 1, 0):
            with pytest.raises(GatewayError) as e:
                c.call("send", conversation_id=CONVERSATION, client_msg_id=f"s{stale}",
                       text="from the past", presence_gen=stale)
            assert e.value.code == "stale_presence"
    assert [r[1] for r in rows(rt.db)] == ["system"]


def test_session_surface_is_bound_at_hello(rt):
    with rt.client(surface="phone") as c:
        # The active body is mac at generation g; phone holding the same number is stale,
        # and no frame field can claim to be mac.
        with pytest.raises(GatewayError) as e:
            c.call("send", conversation_id=CONVERSATION, client_msg_id="p1", text="hi",
                   presence_gen=gen(c), surface="mac")
        assert e.value.code == "stale_presence"
    assert [r[1] for r in rows(rt.db)] == ["system"]


def jack(db: Path, to: str, handoff_id: str) -> None:
    conn = sqlite3.connect(db)
    conn.isolation_level = None
    enqueue_control(conn, "stark", "stark", "jack", {"to": to, "handoff_id": handoff_id})
    conn.close()


def test_jack_out_makes_old_generation_stale_then_reverts_home(rt):
    with rt.client() as mac:
        g = gen(mac)
        # Operator jack toward a surface that never arrives (the demo's `stark.py jack ghost`).
        jack(rt.db, "ghost", "h1")
        pushed = mac.wait_event("presence", lambda d: (d["handoff"] or {}).get("state")
                                == "preparing")
        assert pushed["handoff"]["from"] == "mac"
        out = mac.call("ready_out", handoff_id="h1", draft="half typed", presence_gen=g)
        assert out["state"] == "committed" and out["to_gen"] == g + 1
        with pytest.raises(GatewayError) as e:
            mac.call("send", conversation_id=CONVERSATION, client_msg_id="late", text="late",
                     presence_gen=g)
        assert e.value.code == "stale_presence"
        # ghost never acks: the ack deadline reverts presence home under a newer generation.
        home = mac.wait_event("presence", lambda d: d["presence"]["generation"] == g + 2)
        assert home["presence"]["surface"] == "mac" and home["handoff"] is None
        for stale in (g, g + 1):
            with pytest.raises(GatewayError):
                mac.call("send", conversation_id=CONVERSATION, client_msg_id=f"x{stale}",
                         text="stale", presence_gen=stale)
        mac.call("send", conversation_id=CONVERSATION, client_msg_id="back", text="back home",
                 presence_gen=g + 2)
    assert [r[2] for r in rows(rt.db) if r[1] == "user"] == ["back home"]


def test_ready_out_from_a_non_source_is_rejected(rt):
    with rt.client(surface="ghost") as ghost:
        jack(rt.db, "ghost", "h2")
        wait_for(lambda: ghost.call("snapshot")["handoff"])
        with pytest.raises(GatewayError) as e:
            ghost.call("ready_out", handoff_id="h2", draft=None, presence_gen=1)
        assert e.value.code == "stale_presence"
        assert ghost.call("snapshot")["handoff"]["state"] == "preparing"


def test_unconfirmed_ready_out_retried_after_reconnect_commits_once(rt):
    with rt.client(surface="phone") as phone:            # the live target stays connected
        mac = rt.client()
        g = gen(mac)
        jack(rt.db, "phone", "h3")
        mac.wait_event("presence", lambda d: (d["handoff"] or {}).get("state") == "preparing")
        # The request leaves, the connection drops before its result arrives.
        mac.write({"type": "ready_out", "id": 99, "handoff_id": "h3", "draft": "kept",
                   "presence_gen": g})
        mac.close()
        with rt.client() as again:
            out = again.call("ready_out", handoff_id="h3", draft="kept", presence_gen=g)
            assert (out["state"], out["to_gen"]) == ("committed", g + 1)
            p = again.call("snapshot")["presence"]
            assert (p["surface"], p["generation"]) == ("phone", g + 1)
        # The target's ack is unconfirmed too; its retry answers acked with no new commit.
        phone.write({"type": "ack_in", "id": 98, "handoff_id": "h3", "presence_gen": g + 1})
        out = phone.call("ack_in", handoff_id="h3", presence_gen=g + 1)
        assert (out["state"], out["draft"]) == ("acked", "kept")
        assert phone.call("snapshot")["presence"]["generation"] == g + 1


# ---------------------------------------------------------------------- reconnect and resync
def test_resend_after_reconnect_lands_once(rt):
    with rt.client() as c:
        g = gen(c)
        first = c.call("send", conversation_id=CONVERSATION, client_msg_id="m1", text="once",
                       presence_gen=g)
    with rt.client() as c:
        again = c.call("send", conversation_id=CONVERSATION, client_msg_id="m1", text="once",
                       presence_gen=g)
    assert (first["seq"], again["seq"], again["replayed"]) == (1, 1, True)
    assert [r[2] for r in rows(rt.db)].count("once") == 1


def test_resync_after_disconnect_has_no_gap_or_duplicate(rt):
    with rt.client() as c:
        c.call("sync", conversation_id=CONVERSATION, after_seq=-1)
        c.call("send", conversation_id=CONVERSATION, client_msg_id="m1", text="one",
               presence_gen=gen(c))
        c.wait_event("messages", lambda d: any(r["role"] == "assistant" for r in d["rows"]))
        last = 2
    with rt.client() as c2:
        c2.call("send", conversation_id=CONVERSATION, client_msg_id="m2", text="two",
                presence_gen=gen(c2))
        wait_for(lambda: len(rows(rt.db)) == 5)
    with rt.client() as c3:
        page = c3.call("sync", conversation_id=CONVERSATION, after_seq=last)
        seqs = [r["seq"] for r in page["rows"]]
        assert seqs == [3, 4] and not page["more"]
        merged = {r[0] for r in rows(rt.db)}
        assert merged == {0, 1, 2, 3, 4}


def test_reply_waits_out_a_mid_call_message_and_answers_both(rt):
    rt.gate.close()
    with rt.client() as c:
        g = gen(c)
        c.call("send", conversation_id=CONVERSATION, client_msg_id="a", text="first",
               presence_gen=g)
        assert rt.gate.waiting.wait(5)
        assert c.call("snapshot")["activity"]["working"]
        c.call("send", conversation_id=CONVERSATION, client_msg_id="b", text="second",
               presence_gen=g)
        rt.gate.opened.set()
        wait_for(lambda: [r[1] for r in rows(rt.db)][-1] == "assistant")
        time.sleep(0.5)                       # no late second reply
        snap = c.call("snapshot")
    roles = [r[1] for r in rows(rt.db)]
    assert roles == ["system", "user", "user", "assistant"]
    assert "second" in rows(rt.db)[-1][2]
    resp = {r["id"]: r["state"] for r in snap["responsibilities"]}
    assert resp["chat:stark:main"] == "done"


def test_ping_stays_fast_while_a_reply_is_in_flight(rt):
    rt.gate.close()
    with rt.client() as c:
        c.call("send", conversation_id=CONVERSATION, client_msg_id="slow", text="think",
               presence_gen=gen(c))
        assert rt.gate.waiting.wait(5)
        worst = 0.0
        for _ in range(50):
            t = time.perf_counter()
            c.call("ping")
            worst = max(worst, time.perf_counter() - t)
        assert c.call("snapshot")["activity"]["working"]
        rt.gate.opened.set()
    assert worst < 0.1, f"ping took {worst * 1000:.1f} ms during inference"


def test_restart_keeps_identity_and_conversation(tmp_path):
    sock_dir = short_dir()
    a = Runtime(tmp_path / "runtime.db", sock_dir).start()
    with a.client() as c:
        uuid = c.welcome["identity"]["uuid"]
        c.call("send", conversation_id=CONVERSATION, client_msg_id="m1", text="remember me",
               presence_gen=gen(c))
        wait_for(lambda: len(rows(a.db)) == 3)
    a.close()
    b = Runtime(tmp_path / "runtime.db", sock_dir).start()
    try:
        with b.client() as c:
            assert c.welcome["identity"]["uuid"] == uuid
            page = c.call("sync", conversation_id=CONVERSATION, after_seq=-1)
            assert [r["content"] for r in page["rows"]][1] == "remember me"
            assert len(page["rows"]) == 3
    finally:
        b.close()


def test_sessions_get_bye_on_shutdown(tmp_path):
    r = Runtime(tmp_path / "runtime.db", short_dir(), gate=Gate()).start()
    c = r.client()
    r.close()
    while (msg := c.read())["type"] == "event":
        pass
    assert msg["type"] == "bye"
    assert not r.sock.exists()


# ---------------------------------------------------------------------- draft continuity
def pad_jacks_in(mac, pad, draft: str | None, hid: str = "d1") -> int:
    """pad pulls Stark from the Mac, which carries draft; returns pad's generation."""
    g = gen(pad)
    pad.call("jack", handoff_id=hid, to="self", presence_gen=g)
    to_gen = mac.call("ready_out", handoff_id=hid, presence_gen=g, draft=draft)["to_gen"]
    out = pad.call("ack_in", handoff_id=hid, presence_gen=to_gen)
    assert out["draft"] == draft
    return to_gen


def test_the_target_gets_the_draft_until_it_confirms_receipt(rt):
    with rt.client() as mac, rt.client(surface="pad") as pad:
        g = pad_jacks_in(mac, pad, "half typed")
        assert pad.call("snapshot")["draft"] == {"handoff_id": "d1", "text": "half typed"}
        assert mac.call("snapshot")["draft"] is None                   # the source never
        for who, at in ((mac, g), (mac, g - 1), (pad, g - 1)):
            with pytest.raises(GatewayError) as e:
                who.call("draft_ack", handoff_id="d1", presence_gen=at)
            assert e.value.code == "stale_presence"
        assert pad.call("draft_ack", handoff_id="d1", presence_gen=g)["draft_state"] \
            == "delivered"
        assert pad.call("draft_ack", handoff_id="d1", presence_gen=g)["draft_state"] \
            == "delivered"                                             # replay
        assert pad.call("snapshot")["draft"] is None
        replay = pad.call("ack_in", handoff_id="d1", presence_gen=g)
        assert replay["draft"] is None                                 # never offered twice


def test_an_unconfirmed_draft_is_redelivered_after_reconnect_and_restart(tmp_path):
    sock_dir = short_dir()
    a = Runtime(tmp_path / "runtime.db", sock_dir).start()
    with a.client() as mac, a.client(surface="pad") as pad:
        g = pad_jacks_in(mac, pad, "half typed")                      # pad drops before receipt
    with a.client(surface="pad") as pad:
        assert pad.call("snapshot")["draft"]["text"] == "half typed"
    a.close()
    b = Runtime(tmp_path / "runtime.db", sock_dir).start()
    try:
        with b.client(surface="pad") as pad:
            assert pad.call("snapshot")["draft"]["text"] == "half typed"
            pad.call("draft_ack", handoff_id="d1", presence_gen=g)
        with b.client(surface="pad") as pad:
            assert pad.call("snapshot")["draft"] is None
    finally:
        b.close()


def test_a_jack_without_a_draft_offers_nothing(rt):
    with rt.client() as mac, rt.client(surface="pad") as pad:
        pad_jacks_in(mac, pad, None)
        assert pad.call("snapshot")["draft"] is None
