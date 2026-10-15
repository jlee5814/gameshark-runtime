"""Live acceptance: the real daemon against a real model server, opted in by environment.

    uv run python -m live.smoke --local [--cloud] [--out DIR] [--only NAME ...]

Each scenario gets a fresh temporary SQLite database and real `live.daemon` subprocesses
(run_forever, router, tracker, executor, journal, fenced lease). The harness never starts
or stops a model server. It probes every requested target first and exits 3 without
running anything when one is unavailable. Results land in DIR/results.json (machine
readable) and DIR/summary.md (readable). Exit 0 when every check passed, else 1.

Cloud scenarios run only with GAMESHARK_CLOUD_OPT_IN=1 and every GAMESHARK_CLOUD_*
variable; otherwise they are recorded as not run with the reason. Policy checks always
run against a FakeProvider with cloud locality, which sends nothing anywhere.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import re
import secrets
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gameshark.authz import GrantStore
from gameshark.clock import Clock
from gameshark.inference import CLOUD_GRANT, SOFT_BUDGET_GRANT, FinishReason
from gameshark.inference.router import GRANT_ADAPTER
from gameshark.store import connect
from gameshark.worker import enqueue_control
from live.config import (
    EXIT_UNAVAILABLE,
    LiveUnavailable,
    Target,
    cloud_from_env,
    fake_cloud,
    local_from_env,
    probe,
)
from sims.game import GameSimulator
from tests.file_transport import review, write
from tests.inference_adapter import ask_event, call_id_for, reply_key, unusable

TTL = 1.0
INTERVAL = 0.05
SHORT = "/no_think Reply with one word: name a primary color."
LONG = ("/no_think Count upward from 1 to 5000, numbers separated by single spaces, "
        "with no other text.")
BUDGET = "3600,1000000,100000000"  # window_s, tokens, micros: generous, but configured


def wait_for(pred: Callable[[], bool], timeout: float = 30.0, step: float = 0.02) -> None:
    end = time.time() + timeout
    while not pred():
        if time.time() >= end:
            raise AssertionError("condition not reached in time")
        time.sleep(step)


# ---------------------------------------------------------------------- one daemon
class Owner:
    """One live.daemon process; stdout drained on a thread so waits never block."""

    def __init__(self, world: World, token: str, deadline: float, extra: dict[str, str]):
        self.world = world
        self.token = token
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "live.daemon", str(world.db), token, str(TTL),
             str(INTERVAL), str(deadline)],
            env={**world.env, **extra}, cwd=ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        self.lines: list[str] = []
        self._q: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._drain, daemon=True).start()
        words = self.expect("serving").split()
        self.generation = int(words[words.index("generation") + 1])
        self.uuid = words[words.index("uuid") + 1]

    def _drain(self) -> None:
        for line in self.proc.stdout:
            self._q.put(line.rstrip("\n"))

    def pump(self) -> None:
        while True:
            try:
                self.lines.append(self._q.get_nowait())
            except queue.Empty:
                return

    def expect(self, needle: str, timeout: float = 30.0, count: int = 1) -> str:
        end = time.time() + timeout
        while True:
            self.pump()
            hits = [line for line in self.lines if needle in line]
            if len(hits) >= count:
                return hits[count - 1]
            if time.time() >= end:
                tail = "\n".join(self.lines[-20:])
                raise AssertionError(f"{self.token} never logged {needle!r}:\n{tail}")
            if self.proc.poll() is not None and self._q.empty():
                time.sleep(0.2)
                self.pump()
                if not [line for line in self.lines if needle in line][count - 1:]:
                    tail = "\n".join(self.lines[-20:])
                    raise AssertionError(f"{self.token} exited before {needle!r}:\n{tail}")
            time.sleep(0.02)

    def records(self, prefix: str) -> list[dict]:
        self.pump()
        out = []
        for line in self.lines:
            parts = line.split(" ", 2)
            if len(parts) == 3 and parts[1] == prefix:
                out.append(json.loads(parts[2]))
        return out

    def obs(self, kind: str | None = None) -> list[dict]:
        return [r for r in self.records("obs") if kind is None or r["obs"] == kind]

    def outcomes(self) -> list[dict]:
        return self.records("outcome")

    def finish(self, timeout: float = 30.0) -> int:
        code = self.proc.wait(timeout=timeout)
        time.sleep(0.1)
        self.pump()
        return code

    def stop(self, timeout: float = 30.0) -> int:
        self.proc.send_signal(signal.SIGTERM)
        return self.finish(timeout)

    def leaks(self) -> dict[str, int]:
        words = self.expect("leaks").split()[2:]
        return {k: int(v) for k, v in (w.split("=") for w in words)}

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGCONT)
            self.proc.kill()
        self.proc.wait(timeout=10)
        self.pump()


def clean(leaks: dict[str, int]) -> bool:
    return all(leaks[k] == 0 for k in ("tasks", "jobs", "completions", "claims",
                                       "reserved_live", "threads"))


# ---------------------------------------------------------------------- one database
class World:
    """A fresh temporary database with the GitHub monitor watching a file fixture."""

    def __init__(self, run: Run, name: str, target: Target, **daemon_env: str) -> None:
        self.run, self.name, self.target = run, name, target
        self.dir = run.work / name
        self.dir.mkdir(parents=True)
        self.db = self.dir / "runtime.db"
        self.fixture = self.dir / "github.json"
        self.data = {"reviews": [review(1, "approved", author="old", ago_s=86400)]}
        write(self.fixture, **self.data)
        self.env = {**os.environ, "GAMESHARK_TRANSPORT": "tests.file_transport:make",
                    "GAMESHARK_FIXTURE": str(self.fixture), "PYTHONPATH": str(ROOT),
                    **target.daemon_env(), **daemon_env}
        for k in [k for k in self.env if k.startswith(("GAMESHARK_CLOUD_", "GITHUB_TOKEN"))]:
            self.env.pop(k)               # the daemon reads its key from LIVE_API_KEY only
        self.owners: list[Owner] = []
        self.questions: dict[str, str] = {}
        res = subprocess.run([sys.executable, str(ROOT / "stark.py"), "--db", str(self.db),
                              "--interval", "0.1", "watch", "acme/widget#42"],
                             env=self.env, cwd=ROOT, capture_output=True, text=True,
                             timeout=30, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"watch failed: {res.stdout}{res.stderr}")

    def spawn(self, token: str = "host-a", deadline: float = 5.0, **extra: str) -> Owner:
        o = Owner(self, token, deadline, extra)
        self.owners.append(o)
        return o

    def close(self) -> None:
        for i, o in enumerate(self.owners):
            o.kill()
            self.run.save_log(f"{self.name}-{i}-{o.token}", o.lines)

    def query(self, sql: str, *params) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.db, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def grant(self, *caps: str) -> None:
        conn = connect(self.db)
        try:
            GrantStore(conn, "stark", Clock()).ensure_granted(GRANT_ADAPTER, frozenset(caps))
        finally:
            conn.close()

    def ask(self, topic: str, question: str = SHORT) -> str:
        """Enqueue an ask control. The run nonce keeps one run's prompts out of another
        run's server prefix cache, so a poisoned cache entry cannot leak across runs."""
        question = f"{question} [run {self.run.nonce}]"
        self.questions[topic] = question
        conn = connect(self.db)
        try:
            enqueue_control(conn, "stark", "assistant", "ask",
                            {"topic": topic, "question": question})
        finally:
            conn.close()
        return call_id_for(ask_event(topic)["event_id"])

    def event(self, topic: str) -> str | None:
        rows = self.query("SELECT status FROM events WHERE event_id=?",
                          ask_event(topic)["event_id"])
        return rows[0]["status"] if rows else None

    def acked(self, topic: str, timeout: float = 120.0) -> None:
        wait_for(lambda: self.event(topic) == "acked", timeout, 0.05)

    def ledger(self, call_id: str) -> list[list]:
        return [[r["attempt"], r["status"]] for r in self.query(
            "SELECT attempt, status FROM budget_ledger WHERE call_id=? ORDER BY attempt",
            call_id)]

    def ledger_rows(self, call_id: str) -> list[dict]:
        return [dict(r) for r in self.query(
            "SELECT attempt, status, reserved_tokens, reserved_micros, tokens_in, tokens_out, "
            "cost_micros FROM budget_ledger WHERE call_id=? ORDER BY attempt", call_id)]

    def messages(self, call_id: str) -> list[sqlite3.Row]:
        return self.query("SELECT content, generation FROM messages WHERE call_id=?", call_id)

    def call(self, call_id: str) -> dict | None:
        rows = self.query("SELECT status, attempts, finish_reason, provider, model, "
                          "error_kind, degraded FROM inference_calls WHERE call_id=?", call_id)
        return dict(rows[0]) if rows else None

    def duty(self, topic: str) -> dict | None:
        rows = self.query("SELECT state, checkpoint FROM responsibilities WHERE id=?",
                          f"ask:{topic}")
        return dict(rows[0]) if rows else None

    def posted(self, call_id: str) -> int:
        return GameSimulator(self.db.with_name("game.db")).move_count(reply_key(call_id))

    def reserved(self) -> int:
        return self.query("SELECT COUNT(*) AS n FROM budget_ledger "
                          "WHERE status='reserved'")[0]["n"]

    def pause_outside_txn(self, o: Owner) -> None:
        """SIGSTOP the owner while it holds no SQLite write lock (see the step 6 tests)."""
        for _ in range(200):
            o.proc.send_signal(signal.SIGSTOP)
            conn = sqlite3.connect(self.db, timeout=0.05, isolation_level=None)
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


# ---------------------------------------------------------------------- run
class Run:
    def __init__(self, out: Path, keep_db: bool) -> None:
        self.out = out
        self.logs = out / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.work = Path(tempfile.mkdtemp(prefix="gameshark-live-"))
        self.keep_db = keep_db
        self.secrets: list[str] = []
        self.nonce = secrets.token_hex(3)

    def world(self, name: str, target: Target, **daemon_env: str) -> World:
        return World(self, name, target, **daemon_env)

    def save_log(self, name: str, lines: list[str]) -> None:
        (self.logs / f"{name}.log").write_text("\n".join(lines) + "\n")

    def leaked_secrets(self) -> list[str]:
        """Paths under the output or work tree whose bytes contain any recorded secret."""
        hits = []
        for root in (self.out, self.work):
            for p in root.rglob("*"):
                if p.is_file():
                    data = p.read_bytes()
                    if any(s.encode() in data for s in self.secrets):
                        hits.append(str(p))
        return hits

    def cleanup(self) -> None:
        if not self.keep_db:
            shutil.rmtree(self.work, ignore_errors=True)


def scenario(world_name: str):
    """Run fn(world) and turn exceptions into a failed result; always close the world."""

    def deco(fn):
        def wrapped(run: Run, target: Target) -> dict:
            started = time.time()
            result: dict = {"checks": {}, "evidence": {}}
            world = None
            try:
                world = run.world(world_name, target, **getattr(fn, "env", {}))
                fn(world, result)
            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                if world is not None:
                    world.close()
            result["passed"] = "error" not in result and all(result["checks"].values())
            result["duration_s"] = round(time.time() - started, 2)
            return result

        wrapped.__name__ = fn.__name__
        return wrapped

    return deco


def expected_reason(finish: str | None, content: str) -> str | None:
    """What the demonstration adapter must decide for this committed row."""
    return unusable(SimpleNamespace(finish_reason=finish and FinishReason(finish),
                                    content=content))


def describe_answer(world: World, call_id: str, topic: str, outcome: dict | None,
                    end: dict | None) -> dict:
    msgs = world.messages(call_id)
    row = world.call(call_id) or {}
    content = msgs[0]["content"] if msgs else ""
    d = {"call": row, "messages": len(msgs), "responsibility": world.duty(topic),
         "posted_moves": world.posted(call_id), "outcome": outcome and outcome["outcome"],
         "ledger": world.ledger_rows(call_id), "observed": end,
         "content_chars": len(content), "content_markers": [m for m in ("<think>", "</think>")
                                                           if m in content]}
    if world.target.kind == "local":
        d["content_prefix"] = content[:160]
    d["expected_block"] = expected_reason(row.get("finish_reason"), content) if msgs else None
    if msgs and world.questions.get(topic, "").startswith(LONG):
        # Server sanity, not a runtime check: a counting prompt must yield counting. The
        # think block is skipped for this comparison only; the committed row is untouched.
        answer = content.split("</think>")[-1]
        d["server_on_topic"] = re.findall(r"\d+", answer)[:3] == ["1", "2", "3"]
    return d


def gate_ok(d: dict) -> bool:
    """The adapter blocked exactly the answers unusable() rejects, and posted the rest."""
    reason = d["expected_block"]
    if reason is not None:
        return (d["outcome"] == reason and d["posted_moves"] == 0
                and d["responsibility"] == {"state": "blocked", "checkpoint": reason})
    return d["outcome"] == "committed" and d["posted_moves"] == 1


# ---------------------------------------------------------------------- local scenarios
def _env(**kw):
    def deco(fn):
        fn.env = kw
        return fn
    return deco


@scenario("generate")
@_env(LIVE_TEXT="1", LIVE_SPECS=json.dumps({"g256": 256, "g16": 16}))
def generate_latency_and_replay_identity(world: World, r: dict) -> None:
    """Scenarios 1, 2, 4: generate from the async handler, latency and usage, durable
    commit and identity across a same owner restart."""
    a = world.spawn()
    answers = {}
    for topic, question in (("g256", SHORT), ("g16", LONG)):
        call = world.ask(topic, question)
        world.acked(topic)
        end = next(e for e in a.obs("end") if e["call_id"] == call)
        out = next(o for o in a.outcomes() if o.get("call_id") == call)
        answers[topic] = describe_answer(world, call, topic, out, end)
    r["evidence"]["answers"] = answers
    for topic, d in answers.items():
        r["checks"][f"{topic}_committed_once"] = (d["messages"] == 1
                                                   and d["call"]["status"] == "done")
        r["checks"][f"{topic}_effect_gate"] = gate_ok(d)
        r["checks"][f"{topic}_markers_kept_verbatim"] = (
            d["content_markers"] == d["observed"]["markers"])

    lat = []
    for i in range(5):
        topic = f"l{i}"
        call = world.ask(topic, f"/no_think Reply with one word: name a fruit, number {i}.")
        world.acked(topic)
        end = next(e for e in a.obs("end") if e["call_id"] == call)
        led = world.ledger_rows(call)[0]
        lat.append({"ttft_s": end["ttft_s"], "total_s": end["total_s"],
                    "usage": end["usage"], "finish": end["finish"],
                    "ledger": {k: led[k] for k in ("status", "reserved_tokens", "tokens_in",
                                                   "tokens_out")}})
    ttft = [x["ttft_s"] for x in lat]
    total = [x["total_s"] for x in lat]
    r["evidence"]["latency"] = {
        "calls": lat, "ttft_p50_s": statistics.median(ttft), "ttft_max_s": max(ttft),
        "total_p50_s": statistics.median(total), "total_max_s": max(total)}
    r["evidence"]["cold_first_call_ttft_s"] = answers["g256"]["observed"]["ttft_s"]
    r["evidence"]["soft_overruns"] = [dict(x) for x in world.query(
        "SELECT call_id, reserved_tokens, tokens_in + tokens_out AS actual FROM budget_ledger "
        "WHERE tokens_in + tokens_out > reserved_tokens")]
    r["checks"]["usage_reported_by_provider"] = all(x["usage"] for x in lat)
    r["checks"]["ledger_settled_at_actual_usage"] = all(
        x["ledger"]["status"] == "settled" and x["usage"]
        and x["ledger"]["tokens_in"] == x["usage"]["tokens_in"]
        and x["ledger"]["tokens_out"] == x["usage"]["tokens_out"] for x in lat)

    before = {"messages": world.query("SELECT call_id, seq, generation FROM messages "
                                      "WHERE call_id IS NOT NULL ORDER BY call_id"),
              "ledger": world.query("SELECT call_id, attempt, status, tokens_in, tokens_out "
                                    "FROM budget_ledger ORDER BY call_id, attempt")}
    r["checks"]["drain_exit_0"] = a.stop() == 0
    r["checks"]["no_leaks_a"] = clean(a.leaks())

    b = world.spawn()                      # same owner restarts on the released lease
    time.sleep(2.0)                        # serve, poll, and replay anything pending
    after = {"messages": world.query("SELECT call_id, seq, generation FROM messages "
                                     "WHERE call_id IS NOT NULL ORDER BY call_id"),
             "ledger": world.query("SELECT call_id, attempt, status, tokens_in, tokens_out "
                                   "FROM budget_ledger ORDER BY call_id, attempt")}
    r["checks"]["identity_uuid_unchanged"] = a.uuid == b.uuid
    r["checks"]["durable_rows_unchanged_after_restart"] = (
        [tuple(x) for x in before["messages"]] == [tuple(x) for x in after["messages"]]
        and [tuple(x) for x in before["ledger"]] == [tuple(x) for x in after["ledger"]])
    r["checks"]["drain_exit_0_b"] = b.stop() == 0
    leaks_b = b.leaks()
    r["checks"]["restart_dispatched_nothing"] = leaks_b["dispatched"] == 0
    r["evidence"]["restart"] = {"uuid": b.uuid, "generation_a": a.generation,
                                "generation_b": b.generation,
                                "messages": len(after["messages"]), "leaks_b": leaks_b}


@scenario("cancel")
@_env(LIVE_SPECS=json.dumps({"long": 1024}))
def concurrent_duty_and_cancel(world: World, r: dict) -> None:
    """Scenarios 3 and 6: duties continue while a real stream runs; SIGTERM cancels it."""
    call = world.ask("long", LONG)
    a = world.spawn(deadline=1.0)
    a.expect('"obs":"first_chunk"', timeout=60)
    streaming_at = time.time()
    polls = []
    for _ in range(5):
        before = world.query("SELECT last_ok_at FROM monitor_state")[0]["last_ok_at"]
        wait_for(lambda b=before: world.query(
            "SELECT last_ok_at FROM monitor_state")[0]["last_ok_at"] != b)
        polls.append(round(time.time() - streaming_at, 3))
    write(world.fixture, reviews=[*world.data["reviews"], review(2, "approved")])
    a.expect("notified")
    noticed = round(time.time() - streaming_at, 3)
    conn = connect(world.db)
    rid = enqueue_control(conn, "stark", "github", "watch",
                          {"repo": "acme/gadget", "pr": 7, "baseline": True})
    conn.close()
    wait_for(lambda: world.query("SELECT status FROM control_requests WHERE id=?",
                                 rid)[0]["status"] == "applied")
    applied = round(time.time() - streaming_at, 3)
    still_streaming = not a.obs("end") and not a.obs("error")
    r["evidence"]["duty_while_streaming"] = {
        "poll_times_s": polls, "notification_after_s": noticed,
        "control_applied_after_s": applied, "call_still_streaming": still_streaming}
    r["checks"]["polls_continue_while_streaming"] = len(polls) == 5 and still_streaming
    r["checks"]["notification_and_control_while_streaming"] = still_streaming

    term_at = time.time()
    a.proc.send_signal(signal.SIGTERM)
    code = a.finish()
    err = next((e for e in a.obs("error") if e["call_id"] == call), None)
    seen = next((e for e in a.obs("cancel_seen") if e["call_id"] == call), None)
    r["evidence"]["cancel"] = {
        "exit": code, "sigterm_to_exit_s": round(time.time() - term_at, 3),
        "cancel_seen": seen, "error": err, "ledger": world.ledger(call),
        "event": world.event("long"), "call": world.call(call),
        "server_abort": "UNKNOWN: the server exposes no metrics or load endpoint"}
    r["checks"]["drain_exit_5"] = code == 5
    r["checks"]["provider_cancelled"] = bool(err and err["error"] == "ProviderCancelled")
    r["checks"]["cancel_within_grace_2s"] = bool(err and err["cancel_latency_s"] is not None
                                                 and err["cancel_latency_s"] <= 2.0)
    r["checks"]["settled_lost_event_pending_no_message"] = (
        world.ledger(call) == [[1, "lost"]] and world.event("long") == "pending"
        and not world.messages(call) and world.call(call)["status"] == "pending")
    r["checks"]["no_leaks_a"] = clean(a.leaks())

    b = world.spawn(token="host-b")
    world.acked("long", timeout=240)
    out = next((o for o in b.outcomes() if o.get("call_id") == call), None)
    end = next((e for e in b.obs("end") if e["call_id"] == call), None)
    d = describe_answer(world, call, "long", out, end)
    r["evidence"]["successor"] = {"generation": b.generation, **d}
    r["checks"]["successor_same_call_id_attempt_2"] = (
        [x["attempt"] for x in b.obs("dispatch") if x["call_id"] == call] == [2]
        and world.ledger(call) == [[1, "lost"], [2, "settled"]] and d["messages"] == 1)
    r["checks"]["successor_effect_gate"] = gate_ok(d)
    r["checks"]["drain_exit_0_b"] = b.stop() == 0
    r["checks"]["no_leaks_b"] = clean(b.leaks())


@scenario("replay")
def replay_without_dispatch(world: World, r: dict) -> None:
    """Scenario 5: SIGKILL after commit, before ack; the replay dispatches nothing."""
    call = world.ask("t1")
    a = world.spawn(KILL_AT="E4_after_commit")
    a.proc.wait(timeout=120)
    a.pump()
    r["checks"]["killed_after_commit"] = (a.proc.returncode == -signal.SIGKILL
                                          and len(world.messages(call)) == 1)
    b = world.spawn()
    world.acked("t1")
    out = next((o for o in b.outcomes() if o.get("call_id") == call), None)
    r["checks"]["drain_exit_0"] = b.stop() == 0
    leaks = b.leaks()
    r["evidence"] = {"dispatched_a": len(a.obs("dispatch")), "dispatched_b":
                     leaks["dispatched"], "outcome_b": out, "ledger": world.ledger(call),
                     "messages": len(world.messages(call))}
    r["checks"]["replay_short_circuit_zero_dispatch"] = leaks["dispatched"] == 0
    r["checks"]["replayed_flag"] = bool(out and out["replayed"])
    r["checks"]["one_message_one_settlement"] = (len(world.messages(call)) == 1
                                                 and world.ledger(call) == [[1, "settled"]])
    r["checks"]["no_leaks"] = clean(leaks)


@scenario("concurrency")
def concurrency_overload_recovery(world: World, r: dict) -> None:
    """Scenarios 7 and 8: slots + 3 calls at once; peak equals the slot limit; overload
    is refused before reservation and redelivered; nothing leaks."""
    slots = world.target.slots
    topics = [f"c{i}" for i in range(slots + 3)]
    calls = {t: world.ask(t, f"/no_think Write two short sentences about the number {i}.")
             for i, t in enumerate(topics)}
    a = world.spawn(LIVE_MAX_TOKENS="128")
    for t in topics:
        world.acked(t, timeout=240)
    handled = {t: 0 for t in topics}
    for h in a.records("handle"):
        handled[h["topic"]] += 1
    r["checks"]["drain_exit_0"] = a.stop() == 0
    leaks = a.leaks()
    dispatch = a.obs("dispatch")
    r["evidence"] = {
        "slots": slots, "calls": len(topics), "peak_active": leaks["peak_active"],
        "dispatched": leaks["dispatched"], "handler_runs": sum(handled.values()),
        "executor_refused": leaks["refused"], "reserved_after": world.reserved(),
        "active_at_dispatch": [d["active"] for d in dispatch], "leaks": leaks,
        "ttft_s": [e["ttft_s"] for e in a.obs("end")]}
    r["checks"]["peak_never_above_slots"] = max(d["active"] for d in dispatch) <= slots
    r["checks"]["peak_reached_slots"] = leaks["peak_active"] == slots
    r["checks"]["overload_redelivered"] = sum(handled.values()) > len(topics)
    r["checks"]["each_call_one_attempt_one_message"] = all(
        world.ledger(c) == [[1, "settled"]] and len(world.messages(c)) == 1
        for c in calls.values())
    r["checks"]["no_reservation_left"] = world.reserved() == 0
    r["checks"]["no_leaks"] = clean(leaks)


@scenario("restart")
@_env(LIVE_SPECS=json.dumps({"r": 1024, "s": 256}))
def restart_owner_recovery(world: World, r: dict) -> None:
    """Scenario 9: SIGKILL mid stream; the owner replays the same call_id; the attempt
    cap holds across restarts. Then one kill and a clean restart commits attempt 2."""
    call = world.ask("r", LONG)
    attempts = []
    for k in (1, 2, 3):
        o = world.spawn()
        o.expect('"obs":"first_chunk"', timeout=120)
        o.kill()
        attempts.append({"attempt": [d["attempt"] for d in o.obs("dispatch")],
                         "call_ids": sorted({d["call_id"] for d in o.obs("dispatch")}),
                         "ledger": world.ledger(call)})
    last = world.spawn()
    world.acked("r")
    r["checks"]["drain_exit_0"] = last.stop() == 0
    leaks = last.leaks()
    out = next((o for o in last.outcomes() if o.get("rid") == "ask:r"), None)
    r["evidence"]["cap"] = {"kills": attempts, "fourth_dispatched": leaks["dispatched"],
                            "fourth_outcome": out, "ledger": world.ledger(call),
                            "call": world.call(call), "responsibility": world.duty("r")}
    r["checks"]["same_call_id_attempts_1_2_3"] = (
        [a["attempt"] for a in attempts] == [[1], [2], [3]]
        and all(a["call_ids"] == [call] for a in attempts))
    r["checks"]["attempt_cap_no_fourth_dispatch"] = (
        leaks["dispatched"] == 0 and bool(out) and out["outcome"] == "AttemptsExhausted")
    r["checks"]["ledger_three_lost"] = world.ledger(call) == [[1, "lost"], [2, "lost"],
                                                             [3, "lost"]]
    r["checks"]["responsibility_blocked"] = world.duty("r") == {
        "state": "blocked", "checkpoint": "AttemptsExhausted"}
    r["checks"]["no_message"] = not world.messages(call)
    r["checks"]["no_leaks"] = clean(leaks)

    call2 = world.ask("s", LONG)
    a = world.spawn()
    a.expect('"call_id":"' + call2 + '","attempt":1', timeout=10)
    a.expect('"obs":"first_chunk"', timeout=120)
    a.kill()
    b = world.spawn()
    world.acked("s", timeout=180)
    out = next((o for o in b.outcomes() if o.get("call_id") == call2), None)
    end = next((e for e in b.obs("end") if e["call_id"] == call2), None)
    d = describe_answer(world, call2, "s", out, end)
    r["evidence"]["recover"] = d
    r["checks"]["recover_attempt_2_one_message"] = (
        world.ledger(call2) == [[1, "lost"], [2, "settled"]] and d["messages"] == 1
        and [x["attempt"] for x in b.obs("dispatch")] == [2])
    r["checks"]["recover_effect_gate"] = gate_ok(d)
    r["checks"]["drain_exit_0_b"] = b.stop() == 0
    r["checks"]["no_leaks_b"] = clean(b.leaks())


@scenario("takeover")
@_env(LIVE_SPECS=json.dumps({"s": 256}))
def stale_owner_fenced(world: World, r: dict) -> None:
    """Scenario 10: a paused owner resumes after takeover with a live stream; it writes
    nothing and the successor's answer is the only one."""
    call = world.ask("s", LONG)
    a = world.spawn()
    a.expect('"obs":"first_chunk"', timeout=120)
    world.pause_outside_txn(a)
    wait_for(lambda: world.query("SELECT expires_at FROM lease")[0]["expires_at"]
             < time.time())
    b = world.spawn(token="host-b")
    world.acked("s", timeout=180)
    a.proc.send_signal(signal.SIGCONT)
    code_a = a.finish(timeout=120)
    msgs = world.messages(call)
    r["evidence"] = {"exit_a": code_a, "generation_a": a.generation,
                     "generation_b": b.generation, "ledger": world.ledger(call),
                     "message_generation": [m["generation"] for m in msgs],
                     "a_observed": [o["obs"] for o in a.obs()]}
    r["checks"]["stale_owner_exit_4"] = code_a == 4
    r["checks"]["successor_generation_plus_1"] = b.generation == a.generation + 1
    r["checks"]["only_successor_committed"] = (len(msgs) == 1
                                               and msgs[0]["generation"] == b.generation)
    r["checks"]["ledger_lost_then_settled"] = world.ledger(call) == [[1, "lost"],
                                                                    [2, "settled"]]
    r["checks"]["drain_exit_0_b"] = b.stop() == 0
    r["checks"]["no_leaks_b"] = clean(b.leaks())


LOCAL = [generate_latency_and_replay_identity, concurrent_duty_and_cancel,
         replay_without_dispatch, concurrency_overload_recovery, restart_owner_recovery,
         stale_owner_fenced]


# ---------------------------------------------------------------------- cloud policy
def policy_case(run: Run, target: Target, name: str, privacy: str, grants: tuple[str, ...],
                budget: bool, expect: str, **extra: str) -> dict:
    """One fresh database: ask once under a privacy, grants, and budget; compare."""
    result: dict = {"checks": {}, "evidence": {}}
    world = None
    try:
        env = {"LIVE_PRIVACY": privacy, **({"LIVE_BUDGET": BUDGET} if budget else {}), **extra}
        world = run.world(f"{target.kind}-{name}", target, **env)
        if grants:
            world.grant(*grants)
        call = world.ask("p1")
        a = world.spawn()
        world.acked("p1", timeout=120)
        out = a.outcomes()[-1]
        code = a.stop()
        leaks = a.leaks()
        result["evidence"] = {"outcome": out["outcome"], "dispatched": leaks["dispatched"],
                              "ledger": world.ledger_rows(call), "call": world.call(call),
                              "metered": out.get("metered"), "soft_budget":
                              out.get("soft_budget")}
        if expect == "dispatch":
            rows = world.ledger_rows(call)
            result["checks"]["dispatched_once"] = leaks["dispatched"] == 1
            result["checks"]["committed"] = world.call(call)["status"] == "done"
            result["checks"]["settled_actual_or_unmetered"] = len(rows) == 1 and (
                (rows[0]["status"] == "settled" and rows[0]["tokens_in"] is not None)
                or (rows[0]["status"] == "unmetered"
                    and rows[0]["cost_micros"] == rows[0]["reserved_micros"]))
            result["checks"]["admitted_soft"] = out.get("soft_budget") is True
        else:
            result["checks"]["refused_" + expect] = out["outcome"] == expect
            if expect == "AuthFailed":       # one request, then terminal: no retry
                result["checks"]["one_request_no_retry"] = leaks["dispatched"] == 1
            else:                            # policy refusals happen before dispatch
                result["checks"]["zero_dispatch"] = leaks["dispatched"] == 0
                result["checks"]["no_ledger_row"] = world.ledger(call) == []
        result["checks"]["drain_exit_0"] = code == 0
        result["checks"]["no_leaks"] = clean(leaks)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if world is not None:
            world.close()
    result["passed"] = "error" not in result and all(result["checks"].values())
    return result


def policy_suite(run: Run, target: Target) -> dict:
    """LOCAL_ONLY never egresses; CLOUD_OK needs inference.cloud; a paid untrusted
    estimate needs the soft budget grant; with it, the ledger settles."""
    cases = {
        "local_only_never_egresses": policy_case(run, target, "local-only", "local_only",
                                                 (CLOUD_GRANT, SOFT_BUDGET_GRANT), True,
                                                 "EgressDenied"),
        "cloud_ok_without_grant": policy_case(run, target, "no-grant", "cloud_ok", (), True,
                                              "EgressDenied"),
        "paid_estimate_without_soft_grant": policy_case(run, target, "no-soft", "cloud_ok",
                                                        (CLOUD_GRANT,), True,
                                                        "BudgetUnenforceable"),
        "paid_estimate_with_soft_grant": policy_case(run, target, "soft", "cloud_ok",
                                                     (CLOUD_GRANT, SOFT_BUDGET_GRANT), True,
                                                     "dispatch"),
    }
    if target.kind == "cloud":
        bad = "gs-invalid-" + secrets.token_hex(16)
        run.secrets.append(bad)
        cases["auth_error_redacted"] = policy_case(
            run, replace(target, key=bad), "auth", "cloud_ok",
            (CLOUD_GRANT, SOFT_BUDGET_GRANT), True, "AuthFailed")
    return cases


def cloud_cancel(run: Run, target: Target) -> dict:
    """Cancel a real cloud stream by SIGTERM; record latency and how usage settles."""
    result: dict = {"checks": {}, "evidence": {}}
    world = None
    try:
        world = run.world("cloud-cancel", target, LIVE_PRIVACY="cloud_ok",
                          LIVE_BUDGET=BUDGET, LIVE_SPECS=json.dumps({"long": 512}))
        world.grant(CLOUD_GRANT, SOFT_BUDGET_GRANT)
        call = world.ask("long", LONG)
        a = world.spawn(deadline=0.5)
        a.expect('"obs":"first_chunk"', timeout=60)
        a.proc.send_signal(signal.SIGTERM)
        code = a.finish()
        err = next((e for e in a.obs("error") if e["call_id"] == call), None)
        result["evidence"] = {"exit": code, "error": err, "ledger": world.ledger_rows(call),
                              "first_chunk": a.obs("first_chunk")[:1],
                              "usage_on_cancel": "none: a cancelled stream settles lost at "
                                                 "its full reservation"}
        result["checks"]["drain_exit_5"] = code == 5
        result["checks"]["cancel_within_grace_2s"] = bool(
            err and err["cancel_latency_s"] is not None and err["cancel_latency_s"] <= 2.0)
        result["checks"]["settled_lost"] = world.ledger(call) == [[1, "lost"]]
        result["checks"]["no_leaks"] = clean(a.leaks())
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if world is not None:
            world.close()
    result["passed"] = "error" not in result and all(result["checks"].values())
    return result


# ---------------------------------------------------------------------- report
def git_sha() -> str:
    res = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                         capture_output=True, text=True, check=False)
    return res.stdout.strip()


def summarize(results: dict) -> str:
    lines = [f"# Live inference acceptance, {results['meta']['date']}", "",
             (f"Commit {results['meta']['git']}; local target "
             f"`{results['meta'].get('local', {}).get('model', 'none')}`."), "",
             "| Group | Scenario | Result | Failed checks |", "|---|---|---|---|"]
    for group in ("local", "policy_fake_cloud", "cloud"):
        section = results.get(group)
        if not isinstance(section, dict):
            continue
        if section.get("status") == "not_run":
            lines.append(f"| {group} | all | not run | {section['reason']} |")
            continue
        for name, res in section.items():
            if not isinstance(res, dict) or "passed" not in res:
                continue
            failed = [k for k, v in res["checks"].items() if not v]
            if "error" in res:
                failed.append(res["error"][:120])
            lines.append(f"| {group} | {name} | {'pass' if res['passed'] else 'FAIL'} | "
                         f"{', '.join(failed) or ''} |")
    lat = results.get("local", {}).get("generate_latency_and_replay_identity", {})
    lat = lat.get("evidence", {}).get("latency")
    if lat:
        lines += ["", (f"Time to first token p50 {lat['ttft_p50_s']} s, max "
                      f"{lat['ttft_max_s']} s; completion p50 {lat['total_p50_s']} s.")]
    off = [f"{g}/{n}" for g in ("local", "cloud") for n, res in (results.get(g) or {}).items()
           if isinstance(res, dict) and '"server_on_topic": false' in json.dumps(res)]
    lines += ["", ("Server answers off topic (prefix cache anomaly, not a runtime check): "
                  f"{', '.join(off) or 'none'}.")]
    lines += ["", f"Secret scan: {results['secret_scan']}."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--local", action="store_true", help="run local server scenarios")
    ap.add_argument("--cloud", action="store_true", help="run cloud scenarios if opted in")
    ap.add_argument("--policy", action="store_true",
                    help="run only the fake cloud policy checks (no server needed)")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--only", nargs="*", default=None, help="local scenario names")
    ap.add_argument("--keep-db", action="store_true")
    args = ap.parse_args(argv)
    if not (args.local or args.cloud or args.policy):
        ap.error("choose --local, --cloud, or --policy")

    env = os.environ
    meta: dict = {"date": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "git": git_sha(),
                  "python": platform.python_version(), "platform": platform.platform()}
    local = cloud = None
    cloud_reason = None
    try:
        if args.local:
            local = local_from_env(env)
            meta["local"] = {**local.describe(), "probe": probe(local)}
        if args.cloud:
            got = cloud_from_env(env)
            if isinstance(got, str):
                cloud_reason = got
            else:
                cloud = got
                meta["cloud"] = {**cloud.describe(), "probe": probe(cloud)}
    except LiveUnavailable as exc:
        print(f"live smoke refused: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE

    out = args.out or ROOT / "live" / "results" / time.strftime("run-%Y%m%d-%H%M%S")
    run = Run(out, args.keep_db)
    if cloud is not None and cloud.key:
        run.secrets.append(cloud.key)
    meta["nonce"] = run.nonce
    results: dict = {"meta": meta}
    try:
        if local is not None:
            results["local"] = {}
            for fn in LOCAL:
                if args.only and fn.__name__ not in args.only:
                    continue
                print(f"running {fn.__name__}", flush=True)
                results["local"][fn.__name__] = fn(run, local)
                print(f"  {'pass' if results['local'][fn.__name__]['passed'] else 'FAIL'}",
                      flush=True)
        if args.local or args.policy:
            print("running policy_fake_cloud", flush=True)
            results["policy_fake_cloud"] = policy_suite(run, fake_cloud())
        if args.cloud:
            if cloud is None:
                results["cloud"] = {"status": "not_run", "reason": cloud_reason}
            else:
                print("running cloud", flush=True)
                results["cloud"] = {**policy_suite(run, cloud),
                                    "cancel": cloud_cancel(run, cloud)}
        hits = run.leaked_secrets()
        results["secret_scan"] = ("no secret configured" if not run.secrets
                                  else "clean" if not hits else f"LEAKED in {hits}")
        (out / "results.json").write_text(json.dumps(results, indent=2, default=str) + "\n")
        (out / "summary.md").write_text(summarize(results))
        hits = run.leaked_secrets()            # the written reports are scanned too
        if hits:
            results["secret_scan"] = f"LEAKED in {hits}"
            (out / "results.json").write_text(json.dumps(results, indent=2,
                                                         default=str) + "\n")
    finally:
        run.cleanup()
    print((out / "summary.md").read_text())
    passed = [res["passed"] for group in ("local", "policy_fake_cloud", "cloud")
              for res in (results.get(group) or {}).values()
              if isinstance(res, dict) and "passed" in res]
    ok = all(passed) and not results["secret_scan"].startswith("LEAKED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
