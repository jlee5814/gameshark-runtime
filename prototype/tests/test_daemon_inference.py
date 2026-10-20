"""Acceptance: the real daemon serves inference beside its other duties, and recovers.

Each owner is an `inference_daemon_worker.py` process running run_forever with the GitHub
monitor on the file transport and the async assistant. Provider calls block on gate files
or on a stalled loopback HTTP fixture, so every interleaving is forced by an explicit
signal. Covers: monitoring and controls continuing while a call blocks; SIGKILL at E1 to
E4 and in the middle of an HTTP stream; SIGTERM draining short calls and cancelling long
ones; a provider that ignores cancellation; and a stale owner resuming after takeover.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from gameshark.store import connect
from gameshark.worker import enqueue_control
from sims.game import GameSimulator
from tests.file_transport import review, write
from tests.inference_adapter import ask_event, call_id_for, reply_key
from tests.openai_fixture import OpenAIFixture, Reply, chunk, completion, sse, usage_chunk
from tests.test_daemon import ROOT, query, stark, wait_for
from tests.test_inference_journal import CRASH_POINTS, EXPECT

TTL = 1.0
INTERVAL = 0.05
# A lease no test expires on purpose. The heartbeat starts only after start() returns, so a
# 1.0 s lease can lapse during a successor's startup recovery on a slow runner.
PRODUCTION_TTL = 30.0

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


@pytest.fixture
def env(tmp_path):
    fixture = tmp_path / "github.json"
    gates = tmp_path / "gates"
    gates.mkdir()
    e = {**os.environ, "GAMESHARK_TRANSPORT": "tests.file_transport:make",
         "GAMESHARK_FIXTURE": str(fixture), "PYTHONPATH": str(ROOT),
         "INFERENCE_GATES": str(gates)}
    e.pop("GITHUB_TOKEN", None)
    env = {"env": e, "db": tmp_path / "runtime.db", "fixture": fixture, "gates": gates,
           "data": {"reviews": [review(1, "approved", author="old", ago_s=86400)]}}
    write(fixture, **env["data"])
    assert stark(env, "watch", "acme/widget#42").returncode == 0
    return env


class Owner:
    """One daemon process; stdout drained on a thread so waits never block."""

    def __init__(self, env, token: str, deadline: float = 5.0, ttl: float = TTL,
                 **extra: str) -> None:
        self.token = token
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "tests" / "inference_daemon_worker.py"),
             str(env["db"]), token, str(ttl), str(INTERVAL), str(deadline)],
            env={**env["env"], **extra}, cwd=ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        self.lines: list[str] = []
        self._q: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._drain, daemon=True).start()
        words = self.expect("serving").split()
        self.generation = int(words[words.index("generation") + 1])

    def _drain(self) -> None:
        for line in self.proc.stdout:
            self._q.put(line.rstrip("\n"))

    def expect(self, needle: str, timeout: float = 15.0, count: int = 1) -> str:
        end = time.time() + timeout
        while True:
            hits = [line for line in self.lines if needle in line]
            if len(hits) >= count:
                return hits[count - 1]
            if time.time() >= end:
                raise AssertionError(f"{self.token} never logged {needle!r}: {self.lines}")
            try:
                self.lines.append(self._q.get(timeout=0.05))
            except queue.Empty:
                continue

    def finish(self, timeout: float = 15.0) -> int:
        code = self.proc.wait(timeout=timeout)
        if code in (0, 4, 5):
            self.expect(f"exit {code}")         # run_forever returned; not a hard exit
        return code

    def leaks(self) -> dict[str, int]:
        words = self.expect("leaks").split()[2:]
        return {k: int(v) for k, v in (w.split("=") for w in words)}

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGCONT)
            self.proc.kill()
        self.proc.wait(timeout=10)


@pytest.fixture
def owners():
    started: list[Owner] = []
    yield started
    for o in started:
        o.kill()


def spawn(env, owners, token: str = "host-a", deadline: float = 5.0, ttl: float = TTL,
          **extra) -> Owner:
    o = Owner(env, token, deadline, ttl, **extra)
    owners.append(o)
    return o


def ask(env, topic: str) -> str:
    conn = connect(env["db"])
    try:
        enqueue_control(conn, "stark", "assistant", "ask",
                        {"topic": topic, "question": f"what about {topic}"})
    finally:
        conn.close()
    return call_id_for(ask_event(topic)["event_id"])


def open_gate(env, topic: str) -> None:
    (env["gates"] / f"open-{topic[1:]}").touch()


def event(env, topic: str) -> str | None:
    rows = query(env, "SELECT status FROM events WHERE event_id=?", ask_event(topic)["event_id"])
    return rows[0]["status"] if rows else None


def ledger(env, call_id: str) -> list[tuple]:
    return [tuple(r) for r in query(env, "SELECT attempt, status FROM budget_ledger "
                                         "WHERE call_id=? ORDER BY attempt", call_id)]


def messages(env, call_id: str) -> list[str]:
    return [r["content"] for r in query(env, "SELECT content FROM messages WHERE call_id=?",
                                        call_id)]


def posted(env, call_id: str) -> int:
    return GameSimulator(env["db"].with_name("game.db")).move_count(reply_key(call_id))


def answered_once(env, call_id: str) -> None:
    assert len(messages(env, call_id)) == 1
    assert posted(env, call_id) == 1
    assert query(env, "SELECT status FROM effects WHERE key=?",
                 reply_key(call_id))[0]["status"] == "done"
    assert query(env, "SELECT COUNT(*) AS n FROM budget_ledger "
                      "WHERE status='reserved'")[0]["n"] == 0


def no_leaks(o: Owner, stuck_threads: int = 0) -> None:
    """Nothing left in flight at exit. A thread inside a provider that ignored its cancel
    may outlive close; it holds no connection and nobody reads its completion."""
    leaks = o.leaks()
    assert {k: v for k, v in leaks.items() if k != "peak_completions"} == {
        "tasks": 0, "jobs": 0, "completions": 0, "claims": 0, "reserved_live": 0,
        "threads": stuck_threads}


def pause_outside_txn(env, o: Owner) -> None:
    """SIGSTOP the owner at a moment it holds no SQLite write lock, so a successor can
    still write: stop, probe the lock, and resume and retry if the stop landed inside a
    transaction."""
    for _ in range(100):
        o.proc.send_signal(signal.SIGSTOP)
        conn = sqlite3.connect(env["db"], timeout=0.05, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            return
        except sqlite3.OperationalError:
            o.proc.send_signal(signal.SIGCONT)
            time.sleep(0.01)
        finally:
            conn.close()
    raise AssertionError("owner never paused outside a transaction")


# ---------------------------------------------------------------------- responsiveness
def test_monitoring_and_controls_continue_while_inference_blocks(env, owners, tmp_path):
    a = spawn(env, owners)
    call = ask(env, "t1")
    a.expect("provider waiting on open-1")
    assert ledger(env, call) == [(1, "reserved")] and event(env, "t1") == "pending"

    # The call is blocked in the provider. Everything else the daemon does goes on.
    blocked_at = time.time()
    polls: list[float] = []
    for _ in range(5):
        before = query(env, "SELECT last_ok_at FROM monitor_state")[0]["last_ok_at"]
        wait_for(lambda b=before: query(
            env, "SELECT last_ok_at FROM monitor_state")[0]["last_ok_at"] != b)
        polls.append(time.time())
    write(env["fixture"], reviews=[*env["data"]["reviews"], review(2, "approved")])
    a.expect("notified")
    noticed_at = time.time()
    conn = connect(env["db"])
    rid = enqueue_control(conn, "stark", "github", "watch",
                          {"repo": "acme/gadget", "pr": 7, "baseline": True})
    conn.close()
    wait_for(lambda: query(env, "SELECT status FROM control_requests WHERE id=?",
                           rid)[0]["status"] == "applied")
    applied_at = time.time()
    assert event(env, "t1") == "pending" and messages(env, call) == []
    assert ledger(env, call) == [(1, "reserved")]

    open_gate(env, "t1")
    wait_for(lambda: event(env, "t1") == "acked")
    answered_once(env, call)
    a.proc.send_signal(signal.SIGTERM)
    assert a.finish() == 0
    no_leaks(a)
    evidence = {"polls_while_blocked": len(polls),
                "poll_gaps_s": [round(b - a_, 3) for a_, b in zip([blocked_at, *polls[:-1]],
                                                                  polls, strict=True)],
                "notification_after_s": round(noticed_at - blocked_at, 3),
                "control_applied_after_s": round(applied_at - blocked_at, 3)}
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))
    print(f"\nconcurrent work evidence: {json.dumps(evidence)}")


# ---------------------------------------------------------------------- SIGKILL
@pytest.mark.parametrize("label", CRASH_POINTS)
def test_sigkill_at_crash_point_in_the_daemon_recovers_to_one_answer(env, owners, label):
    call = ask(env, "t1")
    a = spawn(env, owners, INFERENCE_GATES="open", KILL_AT=label)
    a.proc.wait(timeout=15)
    assert a.proc.returncode == -signal.SIGKILL
    uuid = query(env, "SELECT uuid FROM identity")[0]["uuid"]

    b = spawn(env, owners, INFERENCE_GATES="open")
    wait_for(lambda: event(env, "t1") == "acked")
    assert ledger(env, call) == EXPECT[label][2]
    answered_once(env, call)
    assert query(env, "SELECT uuid FROM identity")[0]["uuid"] == uuid
    assert query(env, "SELECT state FROM responsibilities WHERE id='ask:t1'")[0]["state"] == "done"
    b.proc.send_signal(signal.SIGTERM)
    assert b.finish() == 0
    no_leaks(b)


def stalled_stream() -> Reply:
    first = sse(chunk(role="assistant", content=""), chunk("hel"), done=False)
    rest = sse(chunk("lo"), chunk(finish="stop"), usage_chunk(5, 2))
    return Reply(parts=[first, rest], stall_after=1)


def test_sigkill_during_active_http_inference_recovers_without_duplicates(env, owners):
    srv = OpenAIFixture()
    srv.routes["/v1/chat/completions"] = [stalled_stream(),
                                          Reply(parts=[completion(["hello", " world"])])]
    try:
        call = ask(env, "t1")
        a = spawn(env, owners, INFERENCE_URL=f"{srv.base}/v1")
        assert srv.stalled.wait(15)               # mid stream: headers and a token sent
        assert ledger(env, call) == [(1, "reserved")] and messages(env, call) == []
        a.proc.kill()
        a.proc.wait(timeout=10)

        b = spawn(env, owners, INFERENCE_URL=f"{srv.base}/v1")
        wait_for(lambda: event(env, "t1") == "acked")
        assert messages(env, call) == ["hello world"]
        assert ledger(env, call) == [(1, "lost"), (2, "settled")]
        answered_once(env, call)
        chats = srv.chats()
        assert len(chats) == 2 and chats[0]["messages"] == chats[1]["messages"]
        b.proc.send_signal(signal.SIGTERM)
        assert b.finish() == 0
        no_leaks(b)
    finally:
        srv.stop()


# ---------------------------------------------------------------------- SIGTERM
def test_sigterm_drains_a_short_call_and_cancels_a_long_one(env, owners):
    short, long_ = ask(env, "t1"), ask(env, "t2")
    a = spawn(env, owners, deadline=1.5)
    a.expect("provider waiting on open-1")
    a.expect("provider waiting on open-2")
    a.proc.send_signal(signal.SIGTERM)
    a.expect("draining generation")
    open_gate(env, "t1")                          # the short call finishes inside the drain
    assert a.finish() == 5
    assert event(env, "t1") == "acked"
    answered_once(env, short)
    assert event(env, "t2") == "pending" and messages(env, long_) == []
    assert ledger(env, long_) == [(1, "lost")]
    assert query(env, "SELECT status FROM inference_calls WHERE call_id=?",
                 long_)[0]["status"] == "pending"
    assert query(env, "SELECT owner_token FROM lease")[0]["owner_token"] is None
    no_leaks(a)

    b = spawn(env, owners, token="host-b", INFERENCE_GATES="open")
    assert b.generation == a.generation + 1
    wait_for(lambda: event(env, "t2") == "acked")
    assert ledger(env, long_) == [(1, "lost"), (2, "settled")]
    answered_once(env, long_)
    answered_once(env, short)                     # never rerun
    b.proc.send_signal(signal.SIGTERM)
    assert b.finish() == 0


def test_sigterm_with_a_provider_ignoring_cancel_ends_before_the_grace_abort(env, owners):
    call = ask(env, "t2")
    a = spawn(env, owners, deadline=0.5, INFERENCE_DEAF="1")
    a.expect("provider waiting on open-2")
    started = time.time()
    a.proc.send_signal(signal.SIGTERM)
    assert a.finish() == 5                        # not 6: dropped before the hard abort
    assert time.time() - started < 0.5 + 2.0 + 1.0
    a.expect("drain dropped 1 inference calls")
    assert ledger(env, call) == [(1, "lost")] and messages(env, call) == []
    assert event(env, "t2") == "pending"
    assert query(env, "SELECT owner_token FROM lease")[0]["owner_token"] is None
    no_leaks(a, stuck_threads=1)


# ---------------------------------------------------------------------- takeover
def test_stale_owner_completion_after_takeover_writes_nothing(env, owners):
    call = ask(env, "t1")
    a = spawn(env, owners)
    a.expect("provider waiting on open-1")
    pause_outside_txn(env, a)
    wait_for(lambda: query(env, "SELECT expires_at FROM lease")[0]["expires_at"] < time.time())

    # Only host-a's lease expires on purpose. host-b holds a production length lease, so
    # its own startup recovery can never outlast it on a slow runner and fence itself.
    b = spawn(env, owners, token="host-b", ttl=PRODUCTION_TTL, INFERENCE_GATES="open")
    assert b.generation == a.generation + 1
    wait_for(lambda: event(env, "t1") == "acked")
    open_gate(env, "t1")                          # the stale call can now answer
    a.proc.send_signal(signal.SIGCONT)
    assert a.finish() == 4
    a.expect("lost ownership")
    answered_once(env, call)
    assert ledger(env, call) == [(1, "lost"), (2, "settled")]
    assert query(env, "SELECT generation FROM messages WHERE call_id=?",
                 call)[0]["generation"] == b.generation
    b.proc.send_signal(signal.SIGTERM)
    assert b.finish() == 0
    assert not [line for line in b.lines if "lost ownership" in line]
    no_leaks(b)
