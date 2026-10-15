"""LAN gateway for paired devices: pinned TLS 1.3, single use pairing, ECDSA device proofs,
revocation, and phone driven Jack In / Jack Out, against the real run_forever on an in
process coordinator thread. The phone is gameshark.gateway_client.DeviceClient, which
speaks the same frames StarkKit's TLS transport does."""

from __future__ import annotations

import socket
import sqlite3
import ssl
import stat
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark import devices as dv
from gameshark.gateway import encode, read_frame
from gameshark.gateway_client import (
    GatewayError,
    new_device_key,
    pair_device,
    sign,
    tls_connect,
)
from gameshark.net_gateway import FailureLimiter, lan_peer, load_or_create_identity
from gameshark.stark_chat import CONVERSATION
from gameshark.store import connect
from tests.gateway_runtime import Runtime, short_dir
from tests.helpers import AGENT


@pytest.fixture
def rt(tmp_path):
    r = Runtime(tmp_path / "runtime.db", short_dir(), lan=True).start()
    yield r
    r.close()


def operator_pair(rt: Runtime, name: str = "Jae's iPhone", ttl_s: float = 300.0):
    conn = connect(rt.db)
    try:
        return dv.create_pairing(conn, AGENT, name, time.time(), ttl_s)
    finally:
        conn.close()


def paired(rt: Runtime, name: str = "Jae's iPhone"):
    pid, secret = operator_pair(rt, name)
    return pair_device("127.0.0.1", rt.port, rt.identity.pin, pid, secret, name)


def operator_revoke(rt: Runtime, device_id: str) -> None:
    conn = connect(rt.db)
    try:
        assert dv.revoke(conn, AGENT, device_id, time.time())
    finally:
        conn.close()


def rows(db: Path) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT seq, role, content FROM messages WHERE conversation_id=? "
                            "ORDER BY seq", (CONVERSATION,)).fetchall()
    finally:
        conn.close()


def calls(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM inference_calls").fetchone()[0]
    finally:
        conn.close()


def wait_for(pred, timeout: float = 10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if (v := pred()):
            return v
        time.sleep(0.02)
    raise AssertionError("condition never held")


def presence(c) -> dict:
    return c.call("snapshot")["presence"]


def raw_tls(rt: Runtime) -> tuple[ssl.SSLSocket, dict]:
    """A pinned TLS session that has read its challenge and sent nothing."""
    s = tls_connect("127.0.0.1", rt.port, rt.identity.pin)
    return s, read_frame(s, _alive())


def _alive():
    e = threading.Event()
    e.set()
    return e


def ended(s: socket.socket, timeout: float = 5.0) -> dict | None:
    """Read until the server ends the session; return the last frame it sent, if any.
    Fails if the session is still open after timeout."""
    alive = _alive()
    timer = threading.Timer(timeout, alive.clear)
    timer.start()
    last = None
    try:
        while True:
            last = read_frame(s, alive)
    except (ConnectionError, OSError, ssl.SSLError):
        if not alive.is_set():
            raise AssertionError(f"session still open after {timeout} s") from None
        return last
    finally:
        timer.cancel()


def jack_in(rt: Runtime, mac, phone, hid: str = "in1") -> int:
    """Phone presses Jack In; the Mac plays its exit, the phone confirms arrival."""
    g = presence(phone)["generation"]
    assert phone.call("jack", handoff_id=hid, to="self", presence_gen=g)["state"] == "preparing"
    out = mac.call("ready_out", handoff_id=hid, presence_gen=g, draft=None)
    assert out["state"] == "committed"
    phone.call("ack_in", handoff_id=hid, presence_gen=out["to_gen"])
    return out["to_gen"]


# ---------------------------------------------------------------------- identity
def test_server_identity_is_private_and_its_pin_survives_restart(tmp_path):
    d = short_dir()
    a = load_or_create_identity(d)
    for f in (a.key_path, a.cert_path):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert load_or_create_identity(d).pin == a.pin
    a.key_path.chmod(0o644)
    with pytest.raises(PermissionError):
        load_or_create_identity(d)


def test_identity_refuses_a_directory_open_to_others():
    d = short_dir()
    d.chmod(0o755)
    with pytest.raises(PermissionError):
        load_or_create_identity(d)


@pytest.mark.parametrize("ip,ok", [
    ("127.0.0.1", True), ("192.168.1.20", True), ("10.0.0.5", True), ("172.20.1.1", True),
    ("169.254.3.3", True), ("::1", True), ("fe80::1%en0", True), ("fd12::1", True),
    ("::ffff:192.168.1.2", True), ("8.8.8.8", False), ("100.64.0.1", False),
    ("2001:4860::1", False), ("::ffff:8.8.8.8", False), ("garbage", False)])
def test_only_lan_addresses_are_admitted(ip, ok):
    assert lan_peer(ip) is ok


# ---------------------------------------------------------------------- transport
def test_plaintext_client_gets_no_challenge(rt):
    s = socket.create_connection(("127.0.0.1", rt.port), timeout=5)
    s.sendall(encode({"type": "hello", "proto": 1, "surface": "mac"}))
    data = b""
    try:
        while chunk := s.recv(4096):
            data += chunk
    except OSError:
        pass
    s.close()
    assert b"challenge" not in data and b"nonce" not in data


def test_tls12_client_is_refused(rt):
    with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
        tls_connect("127.0.0.1", rt.port, rt.identity.pin,
                    maximum_version=ssl.TLSVersion.TLSv1_2)


def test_wrong_pin_fails_closed_before_any_frame(rt):
    with pytest.raises(GatewayError) as e:
        tls_connect("127.0.0.1", rt.port, bytes(32))
    assert e.value.code == "pin_mismatch"


def test_non_lan_peer_is_dropped_at_accept(tmp_path):
    r = Runtime(tmp_path / "runtime.db", short_dir(), lan=True, allow=lambda _ip: False).start()
    try:
        with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
            tls_connect("127.0.0.1", r.port, r.identity.pin)
    finally:
        r.close()


# ---------------------------------------------------------------------- pairing
def test_pairing_binds_the_session_to_the_registry_surface(rt):
    device_id, surface, key = paired(rt)
    assert surface.startswith("pocket-")
    with rt.device(device_id, key, extra={"surface": "mac"}) as phone:
        assert phone.welcome["surface"] == surface          # G1: the frame cannot pick
        snap = phone.call("snapshot")
        assert snap["you"] == surface and snap["identity"]["uuid"] == rt.uuid
        assert phone.call("sync", conversation_id=CONVERSATION, after_seq=-1)["rows"]


def test_a_pairing_secret_works_once(rt):
    pid, secret = operator_pair(rt)
    pair_device("127.0.0.1", rt.port, rt.identity.pin, pid, secret, "first")
    with pytest.raises(GatewayError) as e:
        pair_device("127.0.0.1", rt.port, rt.identity.pin, pid, secret, "second")
    assert e.value.code == "auth_failed"


def test_wrong_secret_and_expired_pairings_are_refused(rt):
    pid, secret = operator_pair(rt)
    with pytest.raises(GatewayError):
        pair_device("127.0.0.1", rt.port, rt.identity.pin, pid, "x" + secret, "phone")
    pid2, secret2 = operator_pair(rt, ttl_s=0.2)
    time.sleep(0.3)
    with pytest.raises(GatewayError):
        pair_device("127.0.0.1", rt.port, rt.identity.pin, pid2, secret2, "phone")
    conn = connect(rt.db)
    assert dv.list_devices(conn, AGENT) == []
    conn.close()


# ---------------------------------------------------------------------- device proofs
def test_unknown_device_and_forged_key_are_refused(rt):
    device_id, _surface, _key = paired(rt)
    for did, key in (("0" * 32, new_device_key()), (device_id, new_device_key())):
        with pytest.raises(GatewayError) as e:
            rt.device(did, key)
        assert e.value.code == "auth_failed"


def test_a_captured_hello_cannot_open_another_session(rt):
    device_id, _surface, key = paired(rt)
    with rt.device(device_id, key) as phone:
        captured = phone.hello
    s, challenge = raw_tls(rt)
    assert challenge["nonce"] != captured["sig"]
    s.sendall(encode(captured))
    assert ended(s)["code"] == "auth_failed"


def test_the_unix_token_means_nothing_over_tls(rt):
    s, challenge = raw_tls(rt)
    nonce = bytes.fromhex(challenge["nonce"])
    s.sendall(encode({"type": "hello", "proto": 1, "surface": "mac",
                      "mac": sign(rt.token, nonce, "mac", 1)}))
    assert ended(s)["code"] == "auth_failed"


def test_a_unix_surface_cannot_claim_a_device_name(rt):
    with pytest.raises(GatewayError) as e:
        rt.client("pocket-abc123")
    assert e.value.code == "invalid"


def test_requests_before_the_welcome_end_the_session(rt):
    device_id, _surface, key = paired(rt)
    s, challenge = raw_tls(rt)
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    nonce = bytes.fromhex(challenge["nonce"])
    sig = key.sign(dv.device_transcript(nonce, device_id, 1, rt.identity.pin),
                   ec.ECDSA(hashes.SHA256()))
    s.sendall(encode({"type": "hello", "proto": 1, "device_id": device_id, "sig": sig.hex()})
              + encode({"type": "send", "id": 1, "conversation_id": CONVERSATION,
                        "client_msg_id": "early", "text": "early", "presence_gen": 1}))
    ended(s)
    assert "early" not in [r[2] for r in rows(rt.db)]


def test_oversize_frame_after_auth_ends_the_session(rt):
    device_id, _surface, key = paired(rt)
    phone = rt.device(device_id, key)
    phone.sock.sendall(struct.pack(">I", 1 << 20))
    assert ended(phone.sock)["code"] == "bad_frame"


def test_repeated_failures_block_the_address(tmp_path):
    r = Runtime(tmp_path / "runtime.db", short_dir(), lan=True,
                limiter=FailureLimiter(limit=3, window_s=60)).start()
    try:
        device_id, _surface, key = paired(r)
        for _ in range(3):
            with pytest.raises(GatewayError):
                r.device(device_id, new_device_key())
        time.sleep(0.1)
        with pytest.raises((GatewayError, ssl.SSLError, ConnectionError, OSError)):
            r.device(device_id, key)                         # even the right key, for now
    finally:
        r.close()


# ---------------------------------------------------------------------- revocation
def test_revoking_a_live_holder_closes_it_and_returns_presence_home(rt):
    device_id, surface, key = paired(rt)
    with rt.client() as mac:
        phone = rt.device(device_id, key)
        gen = jack_in(rt, mac, phone)
        assert presence(mac) == {"surface": surface, "home": "mac", "generation": gen}
        operator_revoke(rt, device_id)
        mac.call("ping")                                     # any request pumps the coordinator
        ended(phone.sock)
        p = wait_for(lambda: (q := presence(mac))["surface"] == "mac" and q)
        assert p["generation"] == gen + 1
        with pytest.raises(GatewayError) as e:
            rt.device(device_id, key)
        assert e.value.code == "auth_failed"
        mac.call("send", conversation_id=CONVERSATION, client_msg_id="m1", text="back",
                 presence_gen=gen + 1)


# ---------------------------------------------------------------------- jack from the phone
def test_jack_in_and_out_mid_reply_runs_one_inference_and_keeps_one_conversation(rt):
    device_id, surface, key = paired(rt)
    rt.gate.close()
    with rt.client() as mac, rt.device(device_id, key) as phone:
        mac.call("sync", conversation_id=CONVERSATION, after_seq=-1)
        phone.call("sync", conversation_id=CONVERSATION, after_seq=-1)
        mac.call("send", conversation_id=CONVERSATION, client_msg_id="mac-1",
                 text="watch the build", presence_gen=1)
        assert rt.gate.waiting.wait(5)                       # Stark is thinking on the Mac
        gen = jack_in(rt, mac, phone)
        with pytest.raises(GatewayError) as e:                # the Mac is a read only ghost
            mac.call("send", conversation_id=CONVERSATION, client_msg_id="mac-late",
                     text="late", presence_gen=1)
        assert e.value.code == "stale_presence"
        rt.gate.opened.set()                                 # the same call finishes
        reply = phone.wait_event("messages", lambda d: any(r["role"] == "assistant"
                                                           for r in d["rows"]))
        answer = next(r for r in reply["rows"] if r["role"] == "assistant")
        assert "watch the build" in answer["content"]
        phone.call("send", conversation_id=CONVERSATION, client_msg_id=f"{surface}-1",
                   text="from my pocket", presence_gen=gen)
        wait_for(lambda: [r[1] for r in rows(rt.db)][-1] == "assistant")
        # Jack Out: the phone sends Stark home and plays its exit.
        assert phone.call("jack", handoff_id="out1", to="home",
                          presence_gen=gen)["to"] == "mac"
        out = phone.call("ready_out", handoff_id="out1", presence_gen=gen, draft="unsent")
        acked = mac.call("ack_in", handoff_id="out1", presence_gen=out["to_gen"])
        assert acked["draft"] == "unsent"
        mac.call("send", conversation_id=CONVERSATION, client_msg_id="mac-2",
                 text="home again", presence_gen=out["to_gen"])
        wait_for(lambda: [r[1] for r in rows(rt.db)][-1] == "assistant")
        seen = mac.call("sync", conversation_id=CONVERSATION, after_seq=-1)["rows"]
    roles = [r[1] for r in rows(rt.db)]
    assert roles == ["system", "user", "assistant", "user", "assistant", "user", "assistant"]
    assert [r["seq"] for r in seen] == [r[0] for r in rows(rt.db)]
    assert calls(rt.db) == 3                                 # one call per user turn, none redone


def test_phone_jack_authorization(rt):
    device_id, _surface, key = paired(rt)
    with rt.client() as mac, rt.device(device_id, key) as phone:
        for kw, code in (
            ({"to": "home", "presence_gen": 1}, "stale_presence"),   # not the holder
            ({"to": "self", "presence_gen": 0}, "stale_presence"),   # stale view
            ({"to": "sideways", "presence_gen": 1}, "invalid"),
        ):
            with pytest.raises(GatewayError) as e:
                phone.call("jack", handoff_id="j1", **kw)
            assert e.value.code == code
        with pytest.raises(GatewayError) as e:
            mac.call("jack", handoff_id="j2", to="self", presence_gen=1)
        assert e.value.code == "handoff_conflict"
        a = phone.call("jack", handoff_id="j3", to="self", presence_gen=1)
        assert phone.call("jack", handoff_id="j3", to="self", presence_gen=1) == a
        assert mac.call("snapshot")["handoff"]["id"] == "j3"


def test_late_ack_from_the_phone_reverts_home(rt):
    device_id, _surface, key = paired(rt)
    with rt.client() as mac, rt.device(device_id, key) as phone:
        phone.call("jack", handoff_id="in1", to="self", presence_gen=1)
        out = mac.call("ready_out", handoff_id="in1", presence_gen=1, draft=None)
        time.sleep(1.2)                                     # ack deadline is 1 s here
        with pytest.raises(GatewayError) as e:
            phone.call("ack_in", handoff_id="in1", presence_gen=out["to_gen"])
        assert e.value.code == "stale_presence"
        assert presence(mac)["surface"] == "mac"


def test_phone_dropping_mid_jack_reverts_home(rt):
    device_id, _surface, key = paired(rt)
    with rt.client() as mac:
        phone = rt.device(device_id, key)
        phone.call("jack", handoff_id="in1", to="self", presence_gen=1)
        out = mac.call("ready_out", handoff_id="in1", presence_gen=1, draft=None)
        phone.close()
        p = wait_for(lambda: (q := presence(mac))["generation"] == out["to_gen"] + 1 and q)
        assert p["surface"] == "mac"


def test_restart_keeps_pin_device_key_and_presence(tmp_path):
    sock_dir = short_dir()
    r = Runtime(tmp_path / "runtime.db", sock_dir, lan=True).start()
    device_id, surface, key = paired(r)
    with r.client() as mac, r.device(device_id, key) as phone:
        gen = jack_in(r, mac, phone)
    pin = r.identity.pin
    r.close()
    r2 = Runtime(tmp_path / "runtime.db", sock_dir, lan=True).start()
    try:
        assert r2.identity.pin == pin
        with r2.device(device_id, key) as phone:
            assert presence(phone) == {"surface": surface, "home": "mac", "generation": gen}
            phone.call("send", conversation_id=CONVERSATION, client_msg_id="after",
                       text="after restart", presence_gen=gen)
    finally:
        r2.close()


def test_lan_sessions_cannot_starve_the_unix_socket(rt):
    from gameshark.net_gateway import MAX_LAN_SESSIONS

    idle = [socket.create_connection(("127.0.0.1", rt.port), timeout=5)
            for _ in range(MAX_LAN_SESSIONS + 3)]          # never even start TLS
    try:
        time.sleep(0.2)
        with rt.client() as mac:                            # StarkMac still gets in
            assert mac.call("ping")["pong"]
    finally:
        for s in idle:
            s.close()


def test_a_request_right_after_the_welcome_is_answered(rt):
    """Regression: the welcome went out before the session was marked live, so a phone
    that answered it at once was dropped as speaking before authentication."""
    device_id, _surface, key = paired(rt)
    for _ in range(40):
        with rt.device(device_id, key) as phone:
            assert phone.call("ping")["pong"]
