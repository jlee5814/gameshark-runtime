"""Activity projection: spans, inference, pushes, concurrency, failure, restart, and the
read only guarantee, against the real run_forever on an in process coordinator thread."""

from __future__ import annotations

import ast
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.activity import BLOCKED, IDLE, THINKING, WORKING, ActivityTracker, derive
from gameshark.errors import Cancelled, FencedError
from gameshark.github_client import AuthRevoked, Snapshot, TransientOutage
from gameshark.stark_chat import CONVERSATION
from gameshark.worker import enqueue_control
from tests.gateway_runtime import Gate, Runtime, short_dir

ROOT = Path(__file__).resolve().parents[1]
MONITOR = "monitor_pull_request"


# ---------------------------------------------------------------------- tracker
def test_derive_ranks_working_over_thinking_over_blocked():
    assert derive(True, True, True) == WORKING
    assert derive(False, True, True) == THINKING
    assert derive(False, False, True) == BLOCKED
    assert derive(False, False, False) == IDLE


def test_spans_count_and_the_last_close_returns_to_idle():
    t = ActivityTracker()
    begins = []
    t.on_begin = lambda: begins.append(t.view([])["state"])
    with t.work("a", "k"):
        with t.work("a", "k"), t.work("b", "k"):
            v = t.view([])
            assert v["work"] == [{"responsibility_id": "a", "kind": "k", "count": 2},
                                 {"responsibility_id": "b", "kind": "k", "count": 1}]
        assert t.view([])["work"] == [{"responsibility_id": "a", "kind": "k", "count": 1}]
        assert t.working
    assert not t.working and t.view([])["state"] == IDLE
    assert begins == [WORKING]          # pushed once, when the first span opened


@pytest.mark.parametrize("exc", [RuntimeError("boom"), Cancelled("drain"),
                                 FencedError("lost"), TransientOutage("503")])
def test_a_span_closes_on_failure_cancellation_and_ownership_loss(exc):
    t = ActivityTracker()
    with pytest.raises(type(exc)), t.work("a", "k"):
        raise exc
    assert not t.working


def test_inference_comes_from_the_executor_and_working_outranks_it():
    t = ActivityTracker()
    calls = [("call:x:1", "x", "execute")]
    t.calls = lambda: list(calls)
    assert t.view([])["state"] == THINKING
    with t.work("a", "k"):
        v = t.view(["b"])
        assert v["state"] == WORKING and v["inference"][0]["call_id"] == "call:x:1"
    assert t.view(["b"])["state"] == THINKING
    calls.clear()                        # the executor finished, failed, or abandoned it
    assert t.view(["b"])["state"] == BLOCKED
    assert t.view([])["state"] == IDLE


def test_activity_module_is_read_only():
    tree = ast.parse((ROOT / "gameshark" / "activity.py").read_text())
    imported = {(n.module or "") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.startswith("gameshark") or m in {"sqlite3", "os", "subprocess"}
                   for m in imported), imported
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"conn", "execute", "write_txn", "fenced", "transition", "upsert"}


# ---------------------------------------------------------------------- runtime harness
class GatedGitHub:
    """A GitHub client whose snapshot blocks per pull request until released, like a slow
    GET, or raises a queued error."""

    def __init__(self) -> None:
        self.gates: dict[int, threading.Event] = {}
        self.entered: dict[int, threading.Event] = {}
        self.errors: dict[int, Exception] = {}

    def hold(self, pr: int) -> None:
        self.gates[pr] = threading.Event()
        self.entered[pr] = threading.Event()

    def release(self, pr: int) -> None:
        self.gates[pr].set()

    def viewer(self) -> str:
        return "stark-owner"

    def snapshot(self, repo, pr, etags, head_sha, known=None, cursors=None) -> Snapshot:
        if pr in self.entered:
            self.entered[pr].set()
        if pr in self.gates:
            assert self.gates[pr].wait(10), f"pr {pr} never released"
        if pr in self.errors:
            raise self.errors.pop(pr)
        return Snapshot(pr={"number": pr, "state": "open", "merged": False,
                            "head": {"sha": "sha1"}}, head_sha="sha1")


@pytest.fixture
def gh():
    return GatedGitHub()


@pytest.fixture
def rt(tmp_path, gh):
    r = Runtime(tmp_path / "runtime.db", short_dir(), github=gh).start()
    yield r
    for g in gh.gates.values():
        g.set()
    r.close()


def watch(rt: Runtime, pr: int, repo: str = "o/r") -> None:
    conn = sqlite3.connect(rt.db, timeout=5)
    try:
        with conn:
            enqueue_control(conn, "stark", "github", "watch", {"repo": repo, "pr": pr})
    finally:
        conn.close()


def activity_until(c, pred) -> list[dict]:
    """Every activity push, in order, up to and including the first matching pred."""
    seen = []
    buffered = [e for e in c.events if e["kind"] == "activity"]
    c.events[:] = [e for e in c.events if e["kind"] != "activity"]
    for e in buffered:
        seen.append(e["data"])
        if pred(e["data"]):
            return seen
    while True:
        msg = c.read()
        if msg.get("type") != "event":
            continue
        if msg["kind"] != "activity":
            c.events.append(msg)
            continue
        seen.append(msg["data"])
        if pred(msg["data"]):
            return seen


def state(s: str):
    return lambda d: d["state"] == s


def gen(c) -> int:
    return c.call("snapshot")["presence"]["generation"]


# ---------------------------------------------------------------------- simultaneous work
def test_reply_and_poll_together_never_fall_back_to_idle(rt, gh):
    rt.gate.close()
    with rt.client() as c:
        snap = c.call("snapshot")
        assert snap["activity"]["state"] == IDLE and snap["activity"]["boot"]
        c.call("sync", conversation_id=CONVERSATION, after_seq=-1)   # rows get pushed
        c.call("send", conversation_id=CONVERSATION, client_msg_id="m1", text="hello",
               presence_gen=snap["presence"]["generation"])
        assert rt.gate.waiting.wait(5)
        thinking = activity_until(c, state(THINKING))[-1]
        assert thinking["inference"][0]["call_id"] == f"call:{CONVERSATION}:1"
        assert thinking["working"]                         # the v1 reply flag still holds

        gh.hold(7)
        watch(rt, 7)
        assert gh.entered[7].wait(5)
        # The coordinator is stuck inside the poll, yet the push already arrived.
        working = activity_until(c, state(WORKING))[-1]
        assert working["work"] == [{"responsibility_id": "monitor:o/r:7", "kind": MONITOR,
                                    "count": 1}]
        assert working["inference"], "the reply is still generating underneath"
        gh.release(7)
        seen = activity_until(c, state(THINKING))
        assert all(d["state"] != IDLE for d in seen)

        rt.gate.opened.set()
        seen = activity_until(c, state(IDLE))
        assert all(d["state"] in (THINKING, IDLE) for d in seen)
        kinds = [e["kind"] for e in c.events]
        assert "messages" in kinds, "the reply rows arrive before Stark goes idle"


def test_two_responsibilities_stay_working_until_the_last_finishes(rt, gh):
    gh.hold(1)
    gh.hold(2)
    watch(rt, 1)
    watch(rt, 2)
    with rt.client() as c:
        assert gh.entered[1].wait(5)
        first = activity_until(c, state(WORKING))[-1]
        assert first["work"][0]["responsibility_id"] == "monitor:o/r:1"
        gh.release(1)
        assert gh.entered[2].wait(5)
        seen = activity_until(c, lambda d: d["work"] and
                              d["work"][0]["responsibility_id"] == "monitor:o/r:2")
        assert all(d["state"] == WORKING for d in seen), seen
        gh.release(2)
        assert activity_until(c, state(IDLE))[-1]["work"] == []


# ---------------------------------------------------------------------- outcomes
def test_a_failed_poll_ends_its_span(rt, gh):
    gh.hold(3)
    gh.errors[3] = TransientOutage("503 from GitHub")
    watch(rt, 3)
    with rt.client() as c:
        assert gh.entered[3].wait(5)
        activity_until(c, state(WORKING))
        gh.release(3)
        assert activity_until(c, state(IDLE))[-1]["work"] == []


def test_a_revoked_token_ends_working_in_blocked(rt, gh):
    gh.hold(4)
    gh.errors[4] = AuthRevoked(401, "Bad credentials")
    watch(rt, 4)
    with rt.client() as c:
        assert gh.entered[4].wait(5)
        activity_until(c, state(WORKING))
        gh.release(4)
        blocked = activity_until(c, state(BLOCKED))[-1]
        assert blocked["blocked"] == ["monitor:o/r:4"] and blocked["work"] == []


def test_cancelled_reply_leaves_nothing_in_flight_and_restart_rebuilds(tmp_path):
    sock_dir = short_dir()
    gate = Gate()
    gate.close()
    a = Runtime(tmp_path / "runtime.db", sock_dir, gate=gate).start()
    with a.client() as c:
        c.call("send", conversation_id=CONVERSATION, client_msg_id="m1", text="hold on",
               presence_gen=gen(c))
        assert gate.waiting.wait(5)
        first = activity_until(c, state(THINKING))[-1]
    a.stop.set()                      # drain: the deadline cancels the gated call
    a.thread.join(15)
    assert a.sup is not None and a.sup.executor is not None
    assert a.sup.executor.calls() == [], "the cancelled call left the executor"

    gate2 = Gate()
    gate2.close()
    b = Runtime(tmp_path / "runtime.db", sock_dir, gate=gate2).start()
    try:
        assert gate2.waiting.wait(10), "the replayed reply event reissued its call"
        with b.client() as c:
            snap = c.call("snapshot")["activity"]
            assert snap["boot"] != first["boot"]
            assert snap["state"] == THINKING
            assert snap["inference"][0]["call_id"] == first["inference"][0]["call_id"]
            gate2.opened.set()
            activity_until(c, state(IDLE))
    finally:
        b.close()


def test_a_new_session_snapshot_carries_current_activity(rt, gh):
    gh.hold(5)
    watch(rt, 5)
    assert gh.entered[5].wait(5)
    # The coordinator is blocked in the poll; a fresh session queues its snapshot behind
    # it and answers with the state that holds once the coordinator is free again.
    with rt.client() as c:
        gh.release(5)
        snap = c.call("snapshot")["activity"]
        assert snap["state"] == IDLE and snap["work"] == []
