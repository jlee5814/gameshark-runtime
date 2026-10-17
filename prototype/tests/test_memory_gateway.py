"""Memory through the surface gateway: typed frames, presence and device authentication,
pushes, handoffs, the model seeing saved facts, and SIGKILL recovery of the real daemon."""

from __future__ import annotations

import os
import random
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark import devices as dv
from gameshark.gateway_client import GatewayClient, GatewayError, pair_device
from gameshark.inference import ProviderConfig
from gameshark.memory import GRANT_ADAPTER, HEADER, WRITE
from gameshark.stark_chat import CONVERSATION, CompanionFakeProvider
from gameshark.store import connect
from tests.file_transport import write
from tests.gateway_runtime import Runtime, short_dir
from tests.helpers import AGENT

ROOT = Path(__file__).resolve().parents[1]


class Recording:
    """Provider factory that keeps the fake it built, so a test can read its requests."""

    def __init__(self) -> None:
        self.fake: CompanionFakeProvider | None = None

    def __call__(self) -> list[ProviderConfig]:
        self.fake = CompanionFakeProvider(delay_s=0.0)
        return [ProviderConfig(self.fake)]


@pytest.fixture
def rec() -> Recording:
    return Recording()


@pytest.fixture
def rt(tmp_path, rec):
    r = Runtime(tmp_path / "runtime.db", short_dir(), lan=True, providers=rec).start()
    yield r
    r.close()


def gen(c) -> int:
    return c.call("snapshot")["presence"]["generation"]


def remember(c, op: str, text: str, g: int | None = None) -> dict:
    return c.call("memory_remember", op_id=op, text=text,
                  presence_gen=gen(c) if g is None else g)


def listed(c) -> list[dict]:
    return c.call("memory_list")["memories"]


def wait_for(pred, timeout: float = 15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if (v := pred()):
                return v
        except sqlite3.OperationalError:
            pass
        time.sleep(0.02)
    raise AssertionError("condition never held")


def memory_block(req) -> str:
    return next((m.content for m in req.messages if m.content.startswith(HEADER)), "")


def assistant_count(db: Path) -> int:
    conn = sqlite3.connect(db, timeout=5)
    try:
        return conn.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=? AND "
                            "role='assistant'", (CONVERSATION,)).fetchone()[0]
    finally:
        conn.close()


def paired(rt: Runtime, name: str = "Jae's iPhone"):
    conn = connect(rt.db)
    try:
        pid, secret = dv.create_pairing(conn, AGENT, name, time.time(), 300.0)
    finally:
        conn.close()
    return pair_device("127.0.0.1", rt.port, rt.identity.pin, pid, secret, name)


def jack_in(mac, phone, hid: str) -> int:
    g = gen(phone)
    phone.call("jack", handoff_id=hid, to="self", presence_gen=g)
    out = mac.call("ready_out", handoff_id=hid, presence_gen=g, draft=None)
    phone.call("ack_in", handoff_id=hid, presence_gen=out["to_gen"])
    return out["to_gen"]


# ---------------------------------------------------------------------- frames
def test_snapshot_advertises_memory(rt):
    with rt.client() as mac:
        snap = mac.call("snapshot")
        assert "memory" in snap["features"]
        assert snap["memory"] == {"count": 0, "inject": True}


def test_remember_list_correct_forget_round_trip(rt):
    with rt.client() as mac:
        m = remember(mac, "op1", "Allergic to penicillin.")["memory"]
        assert (m["content"], m["revision"], m["source_surface"]) == (
            "Allergic to penicillin.", 1, "mac")
        assert [x["id"] for x in listed(mac)] == [m["id"]]
        fixed = mac.call("memory_correct", op_id="op2", memory_id=m["id"], revision=1,
                         text="Allergic to amoxicillin.", presence_gen=gen(mac))["memory"]
        assert (fixed["id"], fixed["revision"]) == (m["id"], 2)
        gone = mac.call("memory_forget", op_id="op3", memory_id=m["id"])
        assert (gone["state"], gone["replayed"], gone["history_mentions"]) == (
            "forgotten", False, 0)
        assert listed(mac) == []
        assert mac.call("snapshot")["memory"]["count"] == 0


def test_saved_memory_never_enters_the_conversation(rt):
    with rt.client() as mac:
        remember(mac, "op1", "Secret project codename Bluebird.")
        rows = mac.call("sync", conversation_id=CONVERSATION, after_seq=-1)["rows"]
        assert all("Bluebird" not in r["content"] for r in rows)


def test_resent_op_lands_once(rt):
    with rt.client() as mac:
        a = remember(mac, "op1", "x fact")
        b = remember(mac, "op1", "x fact")
        assert (a["replayed"], b["replayed"], a["memory"]["id"]) == (
            False, True, b["memory"]["id"])
        with pytest.raises(GatewayError) as e:
            remember(mac, "op1", "different text")
        assert e.value.code == "invalid"
        assert len(listed(mac)) == 1


def test_stale_presence_cannot_remember_or_correct(rt):
    with rt.client() as mac:
        m = remember(mac, "op1", "v1")["memory"]
        for call in (lambda: remember(mac, "op2", "v2", g=99),
                     lambda: mac.call("memory_correct", op_id="op3", memory_id=m["id"],
                                      revision=1, text="v2", presence_gen=99)):
            with pytest.raises(GatewayError) as e:
                call()
            assert e.value.code == "stale_presence"
        assert [x["content"] for x in listed(mac)] == ["v1"]


def test_revision_conflict_is_typed(rt):
    with rt.client() as mac, rt.client() as other:
        m = remember(mac, "op1", "v1")["memory"]
        g = gen(mac)
        mac.call("memory_correct", op_id="a", memory_id=m["id"], revision=1, text="from a",
                 presence_gen=g)
        with pytest.raises(GatewayError) as e:
            other.call("memory_correct", op_id="b", memory_id=m["id"], revision=1,
                       text="from b", presence_gen=g)
        assert e.value.code == "conflict"
        assert listed(other)[0]["content"] == "from a"


def test_write_grant_revoked_is_forbidden(rt):
    with rt.client() as mac:
        conn = connect(rt.db)
        try:
            conn.execute("INSERT INTO grants(agent_id, adapter, cap, granted, updated) "
                         "VALUES(?,?,?,0,?) ON CONFLICT(agent_id, adapter, cap) DO UPDATE "
                         "SET granted=0", (AGENT, GRANT_ADAPTER, WRITE, time.time()))
        finally:
            conn.close()
        with pytest.raises(GatewayError) as e:
            remember(mac, "op1", "x")
        assert e.value.code == "forbidden"


def test_frames_are_schema_checked(rt):
    with rt.client() as mac:
        g = gen(mac)
        bad = [
            {"op_id": "has space", "text": "x", "presence_gen": g},
            {"op_id": "ok", "text": "", "presence_gen": g},
            {"op_id": "ok", "text": 42, "presence_gen": g},
            {"op_id": "ok", "text": "x" * 2000, "presence_gen": g},
            {"op_id": "ok", "text": "x"},
        ]
        for fields in bad:
            with pytest.raises(GatewayError) as e:
                mac.call("memory_remember", **fields)
            assert e.value.code == "invalid", fields
        with pytest.raises(GatewayError) as e:
            mac.call("memory_forget", op_id="x", memory_id="mem-" + "0" * 32)
        assert e.value.code == "invalid"
        assert listed(mac) == []


def test_pushes_reach_every_session(rt):
    with rt.client() as mac, rt.client("ghost") as watcher:
        watcher.call("snapshot")
        remember(mac, "op1", "pushed fact")
        data = watcher.wait_event("memories", lambda d: len(d["memories"]) == 1)
        assert data["memories"][0]["content"] == "pushed fact"


# ---------------------------------------------------------------------- devices
def test_phone_saves_then_mac_recalls_after_handoff(rt):
    device_id, _surface, key = paired(rt)
    with rt.client() as mac, rt.device(device_id, key) as phone:
        to_gen = jack_in(mac, phone, "h1")
        with pytest.raises(GatewayError):
            remember(mac, "from-mac", "mac is not present", g=to_gen - 1)
        m = remember(phone, "p1", "Parked on level 3.", g=to_gen)["memory"]
        assert m["source_surface"].startswith("pocket-")
        assert m["source_device_id"] == device_id
        assert [x["content"] for x in listed(mac)] == ["Parked on level 3."]
        # Forget needs no presence: the Mac deletes while Stark is on the phone.
        assert mac.call("memory_forget", op_id="m1", memory_id=m["id"])["state"] == "forgotten"
        assert listed(phone) == []


def test_revoked_device_cannot_touch_memory(rt):
    device_id, _surface, key = paired(rt)
    phone = rt.device(device_id, key)
    try:
        listed(phone)
        conn = connect(rt.db)
        try:
            dv.revoke(conn, AGENT, device_id, time.time())
        finally:
            conn.close()
        with rt.client() as mac:
            mac.call("ping")                       # the pump closes the revoked session
        with pytest.raises((GatewayError, OSError, ConnectionError)):
            phone.call("memory_list")
    finally:
        phone.close()
    with pytest.raises(GatewayError):
        rt.device(device_id, key)


# ---------------------------------------------------------------------- inference
def test_the_model_sees_saved_memory_and_not_forgotten_memory(rt, rec):
    with rt.client() as mac:
        m = remember(mac, "op1", "Favorite color is teal.")["memory"]
        mac.call("send", conversation_id=CONVERSATION, client_msg_id="m1",
                 text="what is my favorite color", presence_gen=gen(mac))
        wait_for(lambda: assistant_count(rt.db) == 1)
        assert "teal" in memory_block(rec.fake.invocations[-1])
        gone = mac.call("memory_forget", op_id="op2", memory_id=m["id"])
        # The demo model's reply quoted the saved fact, so history still holds it once.
        assert gone["history_mentions"] == 1
        mac.call("send", conversation_id=CONVERSATION, client_msg_id="m2",
                 text="and now?", presence_gen=gen(mac))
        wait_for(lambda: assistant_count(rt.db) == 2)
        last = rec.fake.invocations[-1]
        assert memory_block(last) == ""                   # retrieval no longer carries it
        # Ordinary history still does: forgetting is not erasure of the conversation.
        assert any("teal" in x.content for x in last.messages if x.role.value == "assistant")


def test_forget_reports_a_fact_still_in_chat_history(rt):
    with rt.client() as mac:
        mac.call("send", conversation_id=CONVERSATION, client_msg_id="m1",
                 text="my badge number is 55821", presence_gen=gen(mac))
        wait_for(lambda: assistant_count(rt.db) == 1)
        m = remember(mac, "op1", "badge number is 55821")["memory"]
        gone = mac.call("memory_forget", op_id="op2", memory_id=m["id"])
        # The user row and the fake's echo both still hold it: forgetting is not erasure.
        assert gone["history_mentions"] == 2


# ---------------------------------------------------------------------- the real daemon
@pytest.fixture
def denv(tmp_path):
    fixture = tmp_path / "github.json"
    write(fixture)
    sock_dir = short_dir()
    e = {k: v for k, v in os.environ.items() if not k.startswith("GAMESHARK_INFERENCE_")}
    for var in ("GITHUB_TOKEN", "STARK_SOCKET", "STARK_TOKEN", "GAMESHARK_DB"):
        e.pop(var, None)
    e.update(GAMESHARK_TRANSPORT="tests.file_transport:make", GAMESHARK_FIXTURE=str(fixture),
             PYTHONPATH=str(ROOT))
    return {"env": e, "db": sock_dir / "runtime.db",
            "sock": sock_dir / "gateway" / "stark.sock",
            "token": sock_dir / "gateway" / "gateway.token"}


def daemon(env) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, str(ROOT / "stark.py"), "--db", str(env["db"]),
                             "--interval", "0.5", "run", "--fake-model", "--fake-delay", "0"],
                            env=env["env"], cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        if "gateway listening" in line:
            return proc
    raise AssertionError("daemon exited before serving the gateway")


def dclient(env) -> GatewayClient:
    return GatewayClient(env["sock"], bytes.fromhex(env["token"].read_text()))


def test_sigkill_keeps_acked_memories_and_resends_land_once(denv):
    d1 = daemon(denv)
    with dclient(denv) as c:
        uuid = c.welcome["identity"]["uuid"]
        g = gen(c)
        acked = [remember(c, f"op{i}", f"durable fact {i}", g)["memory"]["id"]
                 for i in range(5)]
        # Fire more without waiting for answers, then kill mid stream.
        for i in range(5, 25):
            c.write({"type": "memory_remember", "id": 1000 + i, "op_id": f"op{i}",
                     "text": f"durable fact {i}", "presence_gen": g})
        time.sleep(random.uniform(0.0, 0.05))
    d1.send_signal(signal.SIGKILL)
    d1.wait(15)

    d2 = daemon(denv)
    try:
        with dclient(denv) as c:
            assert c.welcome["identity"]["uuid"] == uuid
            have = {m["id"] for m in listed(c)}
            assert set(acked) <= have
            g = gen(c)
            for i in range(25):          # the surface resends every op it never saw acked
                remember(c, f"op{i}", f"durable fact {i}", g)
            final = listed(c)
            assert len(final) == 25
            assert len({m["content"] for m in final}) == 25
    finally:
        d2.send_signal(signal.SIGTERM)
        assert d2.wait(15) == 0


def test_forget_survives_sigkill_and_restart(denv):
    d1 = daemon(denv)
    with dclient(denv) as c:
        m = remember(c, "op1", "temporary fact 3141")["memory"]
        c.call("memory_forget", op_id="op2", memory_id=m["id"])
    d1.send_signal(signal.SIGKILL)
    d1.wait(15)
    for f in denv["db"].parent.iterdir():
        if f.name.startswith(denv["db"].name):
            assert b"temporary fact 3141" not in f.read_bytes(), f.name
    d2 = daemon(denv)
    try:
        with dclient(denv) as c:
            assert listed(c) == []
    finally:
        d2.send_signal(signal.SIGTERM)
        d2.wait(15)


def test_cli_lists_and_forgets_through_the_daemon(denv):
    d1 = daemon(denv)
    try:
        with dclient(denv) as c:
            m = remember(c, "op1", "cli visible fact")["memory"]
        run = [sys.executable, str(ROOT / "stark.py"), "--db", str(denv["db"]), "memory"]
        out = subprocess.run([*run, "list"], env=denv["env"], cwd=ROOT, capture_output=True,
                             text=True, timeout=30, check=True)
        assert m["id"] in out.stdout and "cli visible fact" in out.stdout
        subprocess.run([*run, "forget", m["id"]], env=denv["env"], cwd=ROOT,
                       capture_output=True, text=True, timeout=30, check=True)
        with dclient(denv) as c:
            wait_for(lambda: listed(c) == [])
    finally:
        d1.send_signal(signal.SIGTERM)
        d1.wait(15)


def test_an_operator_inject_revocation_reaches_live_surfaces(rt):
    with rt.client() as mac:
        mac.call("snapshot")
        remember(mac, "op1", "a fact")
        mac.wait_event("memories", lambda d: d["inject"] and len(d["memories"]) == 1)
        conn = connect(rt.db)
        try:
            conn.execute("UPDATE grants SET granted=0 WHERE adapter='memory' AND "
                         "cap='memory.inject'")
        finally:
            conn.close()
        mac.call("ping")                                  # the pump notices the grant
        assert mac.wait_event("memories", lambda d: not d["inject"])["memories"]
