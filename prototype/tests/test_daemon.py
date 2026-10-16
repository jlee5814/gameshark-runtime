"""Acceptance: Stark keeps a GitHub monitoring responsibility across process restarts
with no second prompt.

Drives the real `stark.py` CLI in subprocesses. One `watch` assigns the responsibility.
A daemon delivers an update, dies by SIGKILL, and a fresh `stark run` with no other
input recovers identity, responsibility, and seen state, delivers only the new activity,
and never repeats the old.
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

from tests.file_transport import check_run, review, write

ROOT = Path(__file__).resolve().parents[1]
TARGET = "acme/widget#42"
RID = "monitor:acme/widget:42"


@pytest.fixture
def env(tmp_path):
    fixture = tmp_path / "github.json"
    write(fixture, reviews=[review(1, "approved", author="old", ago_s=86400)])
    e = {**os.environ, "GAMESHARK_TRANSPORT": "tests.file_transport:make",
         "GAMESHARK_FIXTURE": str(fixture), "PYTHONPATH": str(ROOT)}
    e.pop("GITHUB_TOKEN", None)
    return {"env": e, "db": tmp_path / "runtime.db", "fixture": fixture}


def stark(env, *args, **kw):
    return subprocess.run([sys.executable, str(ROOT / "stark.py"), "--db", str(env["db"]),
                           "--interval", "0.1", *args], env=env["env"], capture_output=True,
                          check=False, text=True, timeout=30, cwd=ROOT, **kw)


def daemon(env) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, str(ROOT / "stark.py"), "--db", str(env["db"]),
                             "--interval", "0.1", "run", "--fake-model"], env=env["env"], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def query(env, sql, *params):
    conn = sqlite3.connect(env["db"], timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def notes(env):
    return [r["op_key"] for r in query(env, "SELECT op_key FROM notifications ORDER BY created")]


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


def started(proc: subprocess.Popen) -> str:
    """Block until the daemon logs recovery complete; return that line."""
    for line in proc.stdout:
        if "resuming" in line:
            return line
    raise AssertionError("daemon exited before recovery")


def wait_ticks(env, n: int = 3) -> None:
    """Wait for n completed polls, observed as last_ok_at advancing."""
    for _ in range(n):
        before = query(env, "SELECT last_ok_at FROM monitor_state")[0]["last_ok_at"]
        wait_for(lambda b=before: query(
            env, "SELECT last_ok_at FROM monitor_state")[0]["last_ok_at"] != b)


def test_responsibility_survives_sigkill_without_second_prompt(env):
    out = stark(env, "watch", TARGET)                 # the only prompt
    assert out.returncode == 0, out.stderr
    assert "applied watch" in out.stdout

    d1 = daemon(env)
    wait_for(lambda: query(env, "SELECT 1 FROM monitor_seen WHERE event_id='review:1'"))
    assert notes(env) == []                           # baseline: history not replayed
    write(env["fixture"], reviews=[review(1, "approved", author="old", ago_s=86400),
                                   review(2, "approved")])
    wait_for(lambda: len(notes(env)) == 1)
    uuid = query(env, "SELECT uuid FROM identity")[0]["uuid"]
    d1.send_signal(signal.SIGKILL)
    d1.wait(timeout=10)
    assert d1.returncode == -signal.SIGKILL

    # Activity while Stark is dead.
    write(env["fixture"], reviews=[review(1, "approved", author="old", ago_s=86400),
                                   review(2, "approved"), review(3, "changes_requested")])

    d2 = daemon(env)                                  # no watch, no other input
    wait_for(lambda: len(notes(env)) == 2)
    wait_ticks(env)                                   # several more polls: nothing repeats
    assert notes(env) == ["notify:acme/widget:42:new_review:review:2",
                          "notify:acme/widget:42:change_request:review:3"]
    assert query(env, "SELECT uuid FROM identity")[0]["uuid"] == uuid
    assert query(env, "SELECT state FROM responsibilities WHERE id=?", RID)[0]["state"] == "active"

    d2.send_signal(signal.SIGTERM)                    # graceful stop releases the lease
    log, _ = d2.communicate(timeout=10)
    assert d2.returncode == 0, log
    assert "resuming 1 responsibilities" in log
    assert query(env, "SELECT owner_token FROM lease")[0]["owner_token"] is None


def test_second_daemon_refused_and_cli_requests_reach_running_daemon(env):
    assert stark(env, "watch", TARGET).returncode == 0
    d1 = daemon(env)
    try:
        wait_for(lambda: query(env, "SELECT 1 FROM monitor_seen"))
        second = stark(env, "run", "--fake-model")
        assert second.returncode == 3 and "already running" in second.stderr

        out = stark(env, "watch", "acme/gadget#7")    # daemon holds the lock: queued
        assert "queued" in out.stdout
        wait_for(lambda: query(env, "SELECT 1 FROM responsibilities "
                                    "WHERE id='monitor:acme/gadget:7'"))
        status = stark(env, "status").stdout
        assert "daemon    running" in status and "monitor:acme/gadget:7" in status
    finally:
        d1.send_signal(signal.SIGTERM)
        d1.wait(timeout=10)


def test_token_revocation_blocks_survives_restart_and_resumes(env):
    assert stark(env, "watch", TARGET).returncode == 0
    write(env["fixture"], revoked=True)
    d1 = daemon(env)
    wait_for(lambda: query(env, "SELECT state FROM responsibilities")[0]["state"] == "blocked")
    # The kill may land before or after the notice commits; either way the inbox holds
    # the auth event, so the restart below ends with exactly one notice.
    d1.send_signal(signal.SIGKILL)
    d1.wait(timeout=10)

    write(env["fixture"], reviews=[review(5, "approved")])
    d2 = daemon(env)
    started(d2)                                       # recovery, including inbox replay, done
    time.sleep(0.5)                                   # blocked: ticks poll nothing
    assert query(env, "SELECT state FROM responsibilities")[0]["state"] == "blocked"
    assert [k.split(":")[-2] for k in notes(env)] == ["auth_revoked"]   # no repeat notice

    assert "queued" in stark(env, "resume", TARGET).stdout
    wait_for(lambda: len(notes(env)) == 2)
    assert query(env, "SELECT state FROM responsibilities")[0]["state"] == "active"
    d2.send_signal(signal.SIGTERM)
    d2.wait(timeout=10)

    feed = stark(env, "feed").stdout
    assert "approved" in feed
    assert "marked 2 read" in stark(env, "ack", "--all").stdout
    assert stark(env, "feed").stdout.strip() == "(no updates)"


def test_actionable_event_survives_restarts_until_acked(env):
    """Full chain: event, poll, classify, durable notification, feed, ack, across kills."""
    assert stark(env, "watch", TARGET).returncode == 0
    d1 = daemon(env)
    wait_for(lambda: query(env, "SELECT 1 FROM monitor_seen"))
    write(env["fixture"], reviews=[review(1, "approved", author="old", ago_s=86400)],
          check_runs=[check_run(77, "failure")])
    wait_for(lambda: len(notes(env)) == 1)
    d1.send_signal(signal.SIGKILL)
    d1.wait(timeout=10)

    def unread():
        return query(env, "SELECT rowid, op_key FROM notifications WHERE read_at IS NULL")

    first = [tuple(r) for r in unread()]
    assert first == [(1, "notify:acme/widget:42:ci_failure:check:77:failure")]

    d2 = daemon(env)                                  # killed before ack: still waiting
    started(d2)
    wait_ticks(env)
    assert [tuple(r) for r in unread()] == first
    assert "test failure" in stark(env, "feed").stdout

    assert "marked 1 read" in stark(env, "ack", "1").stdout
    d2.send_signal(signal.SIGKILL)
    d2.wait(timeout=10)

    d3 = daemon(env)                                  # acked: stays read, never redelivered
    started(d3)
    wait_ticks(env)
    assert unread() == []
    assert len(notes(env)) == 1
    assert stark(env, "feed").stdout.strip() == "(no updates)"
    d3.send_signal(signal.SIGTERM)
    assert d3.wait(timeout=10) == 0
