"""Acceptance: the real `stark.py run` daemon serves surfaces across process death.

A surface sends through the socket, the daemon dies by SIGKILL, and a fresh daemon on the
same database answers with the same identity, the same conversation, and no duplicate
when the surface resends. The jack control reaches the running daemon through the CLI,
and the old presence generation is refused while the inference reply still lands.
"""

from __future__ import annotations

import json
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


# ---------------------------------------------------------------------- paired phone
def lan(env) -> dict:
    info = env["sock"].parent / "lan.json"
    wait_for(info.exists)
    return json.loads(info.read_text())


def cli(env, *args: str) -> str:
    out = subprocess.run([sys.executable, str(ROOT / "stark.py"), "--db", str(env["db"]),
                          *args], env=env["env"], cwd=ROOT, capture_output=True, text=True,
                         timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_cli_paired_phone_survives_sigkill_and_cli_revocation(env):
    from urllib.parse import parse_qs, urlsplit

    from gameshark.gateway_client import DeviceClient, pair_device

    proc = daemon(env, 1.5, "--lan", "--lan-port", "0")
    try:
        info = lan(env)
        link = json.loads(cli(env, "pair", "--name", "Test Phone", "--json",
                              "--address", "127.0.0.1"))["link"]
        q = {k: v[0] for k, v in parse_qs(urlsplit(link).query).items()}
        assert (int(q["p"]), q["fp"]) == (info["port"], info["pin"])
        pin = bytes.fromhex(q["fp"])
        device_id, surface, key = pair_device(q["a"], int(q["p"]), pin, q["id"], q["s"],
                                              "Test Phone")
        assert device_id in cli(env, "devices")
        with client(env) as mac, DeviceClient("127.0.0.1", info["port"], pin, device_id,
                                              key) as phone:
            phone.call("jack", handoff_id="in1", to="self", presence_gen=1)
            out = mac.call("ready_out", handoff_id="in1", presence_gen=1, draft=None)
            phone.call("ack_in", handoff_id="in1", presence_gen=out["to_gen"])
            gen = out["to_gen"]
            sent = phone.call("send", conversation_id=CONVERSATION, client_msg_id="p1",
                              text="from the phone", presence_gen=gen)
        assert stop(proc, signal.SIGKILL) == -signal.SIGKILL      # mid reply
        proc = daemon(env, 0.1, "--lan", "--lan-port", "0")
        wait_for(lambda: lan(env)["port"] != info["port"])     # rewritten by this boot
        info2 = lan(env)
        assert info2["pin"] == info["pin"]                        # same pinned key
        with DeviceClient("127.0.0.1", info2["port"], pin, device_id, key) as phone:
            snap = phone.call("snapshot")
            assert snap["presence"] == {"surface": surface, "home": "mac", "generation": gen}
            again = phone.call("send", conversation_id=CONVERSATION, client_msg_id="p1",
                               text="from the phone", presence_gen=gen)
            assert again == {**sent, "replayed": True}
            wait_for(lambda: rows(env)[-1][1] == "assistant")
            assert [r[1] for r in rows(env)] == ["system", "user", "assistant"]
            cli(env, "devices", "revoke", device_id)
            with pytest.raises((GatewayError, ConnectionError, OSError)):
                for _ in range(50):
                    phone.call("ping")
                    time.sleep(0.05)
        with client(env) as mac:
            wait_for(lambda: mac.call("snapshot")["presence"]["surface"] == "mac")
        with pytest.raises(GatewayError):
            DeviceClient("127.0.0.1", info2["port"], pin, device_id, key)
        assert "revoked" in cli(env, "devices")
    finally:
        if proc.poll() is None:
            stop(proc)


def test_busy_lan_port_disables_the_whole_gateway(env):
    """No half started gateway: when the LAN port is taken, the Unix socket closes too and
    no lan.json advertises an endpoint. The daemon keeps its duties."""
    import socket

    env["sock"].parent.mkdir(mode=0o700, exist_ok=True)
    stale = env["sock"].parent / "lan.json"
    stale.write_text(json.dumps({"port": 1, "pin": "00" * 32}))
    busy = socket.create_server(("", 0), family=socket.AF_INET6, dualstack_ipv6=True)
    proc = subprocess.Popen([sys.executable, str(ROOT / "stark.py"), "--db", str(env["db"]),
                             "--interval", "0.5", "run", "--lan", "--lan-port",
                             str(busy.getsockname()[1])],
                            env=env["env"], cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        for line in proc.stdout:
            assert "gateway listening" not in line
            if "gateway disabled" in line:
                break
        else:
            raise AssertionError("daemon exited without reporting the gateway")
        assert not env["sock"].exists()
        assert not stale.exists()
        time.sleep(1.0)
        assert proc.poll() is None                                # duties keep running
    finally:
        busy.close()
        if proc.poll() is None:
            stop(proc)
