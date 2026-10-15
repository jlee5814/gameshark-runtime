"""Acceptance: the real `stark.py run` daemon serves surfaces across process death.

A surface sends through the socket, the daemon dies by SIGKILL, and a fresh daemon on the
same database answers with the same identity, the same conversation, and no duplicate
when the surface resends. The jack control reaches the running daemon through the CLI,
and the old presence generation is refused while the inference reply still lands.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.gateway_client import GatewayClient, GatewayError
from gameshark.stark_chat import CONVERSATION
from tests.file_transport import write
from tests.gateway_runtime import short_dir

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(tmp_path):
    fixture = tmp_path / "github.json"
    write(fixture)
    sock_dir = short_dir()
    db = sock_dir / "runtime.db"      # the socket path stays short
    e = {**os.environ, "GAMESHARK_TRANSPORT": "tests.file_transport:make",
         "GAMESHARK_FIXTURE": str(fixture), "PYTHONPATH": str(ROOT)}
    for var in ("GITHUB_TOKEN", "STARK_SOCKET", "STARK_TOKEN"):
        e.pop(var, None)
    return {"env": e, "db": db, "sock": sock_dir / "gateway" / "stark.sock",
            "token": sock_dir / "gateway" / "gateway.token"}


def daemon(env, delay: float = 0.2, *extra: str) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, str(ROOT / "stark.py"), "--db", str(env["db"]),
                             "--interval", "0.5", "run", "--fake-delay", str(delay), *extra],
                            env=env["env"], cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        if "gateway listening" in line:
            return proc
    raise AssertionError("daemon exited before serving the gateway")


def client(env) -> GatewayClient:
    return GatewayClient(env["sock"], bytes.fromhex(env["token"].read_text()))


def rows(env) -> list[tuple]:
    conn = sqlite3.connect(env["db"], timeout=5)
    try:
        return conn.execute("SELECT seq, role, content FROM messages WHERE conversation_id=? "
                            "ORDER BY seq", (CONVERSATION,)).fetchall()
    finally:
        conn.close()


def wait_for(pred, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return
        except sqlite3.OperationalError:
            pass
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def stop(proc: subprocess.Popen, sig=signal.SIGTERM) -> int:
    proc.send_signal(sig)
    return proc.wait(timeout=15)


def test_sigkill_restart_keeps_identity_conversation_and_dedup(env):
    d1 = daemon(env)
    with client(env) as c:
        uuid = c.welcome["identity"]["uuid"]
        g = c.call("snapshot")["presence"]["generation"]
        c.call("send", conversation_id=CONVERSATION, client_msg_id="m1", text="survive this",
               presence_gen=g)
        wait_for(lambda: [r[1] for r in rows(env)][-1] == "assistant")
    stop(d1, signal.SIGKILL)

    d2 = daemon(env)
    try:
        with client(env) as c:
            assert c.welcome["identity"]["uuid"] == uuid
            again = c.call("send", conversation_id=CONVERSATION, client_msg_id="m1",
                           text="survive this", presence_gen=g)
            assert again["replayed"] and again["seq"] == 1
            page = c.call("sync", conversation_id=CONVERSATION, after_seq=-1)
            assert [r["role"] for r in page["rows"]] == ["system", "user", "assistant"]
    finally:
        assert stop(d2) == 0


def test_cli_jack_makes_old_generation_stale_and_reply_still_lands(env):
    d = daemon(env, delay=1.0)
    try:
        with client(env) as c:
            g = c.call("snapshot")["presence"]["generation"]
            c.call("send", conversation_id=CONVERSATION, client_msg_id="m1", text="busy",
                   presence_gen=g)
            out = subprocess.run([sys.executable, str(ROOT / "stark.py"), "--db",
                                  str(env["db"]), "jack", "ghost"], env=env["env"], cwd=ROOT,
                                 capture_output=True, text=True, timeout=30, check=False)
            assert "queued" in out.stdout
            # The silent source commits after the prepare deadline: generation g + 1 is ghost.
            wait_for(lambda: c.call("snapshot")["presence"]["generation"] > g)
            with pytest.raises(GatewayError) as e:
                c.call("send", conversation_id=CONVERSATION, client_msg_id="m2",
                       text="obsolete", presence_gen=g)
            assert e.value.code == "stale_presence"
            wait_for(lambda: [r[1] for r in rows(env)][-1] == "assistant")
            # ghost never acks, so presence reverts home under a newer generation.
            wait_for(lambda: c.call("snapshot")["presence"]["surface"] == "mac"
                     and c.call("snapshot")["presence"]["generation"] == g + 2, timeout=20)
        assert [r[2] for r in rows(env) if r[1] == "user"] == ["busy"]
    finally:
        assert stop(d) == 0


def test_custom_socket_carries_its_token_and_leaves_the_db_dir_alone(env):
    os.chmod(env["db"].parent, 0o755)
    custom = short_dir() / "custom.sock"
    proc = daemon(env, 0.2, "--socket", str(custom))
    try:
        # A client told only the socket finds the token beside it, as StarkKit does.
        c = GatewayClient(custom, bytes.fromhex((custom.parent / "gateway.token").read_text()))
        assert c.call("ping")["pong"]
        c.close()
        assert not env["token"].exists() and not env["sock"].exists()
        assert env["db"].parent.stat().st_mode & 0o777 == 0o755
    finally:
        proc.terminate()
        proc.wait(15)
