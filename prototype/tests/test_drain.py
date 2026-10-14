"""Acceptance: graceful drain and fenced ownership handoff between real processes.

Each owner is a separate `drain_worker.py` process running the real run_forever loop with
the GitHub monitor on a file backed transport. The notifier sleeps per notification, so a
SIGTERM lands while work is in flight. The tests cover a drain that finishes, a drain
cancelled at its deadline, a non cooperative stall aborted at the grace limit, SIGKILL in
the middle of a drain, and a stale owner that resumes after a successor took over. In
every case each logical event notifies exactly once and every effect ends done.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time

import pytest

from gameshark.clock import Clock
from gameshark.errors import LeaseDenied
from gameshark.lease import LeaseManager
from gameshark.store import connect
from tests.file_transport import review, write
from tests.test_daemon import ROOT, query, stark, wait_for

TTL = 1.0
INTERVAL = 0.05

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


@pytest.fixture
def env(tmp_path):
    fixture = tmp_path / "github.json"
    e = {**os.environ, "GAMESHARK_TRANSPORT": "tests.file_transport:make",
         "GAMESHARK_FIXTURE": str(fixture), "PYTHONPATH": str(ROOT)}
    e.pop("GITHUB_TOKEN", None)
    env = {"env": e, "db": tmp_path / "runtime.db", "fixture": fixture,
           "data": {"reviews": [review(1, "approved", author="old", ago_s=86400)]}}
    put(env)
    assert stark(env, "watch", "acme/widget#42").returncode == 0
    return env


def put(env, *reviews, **fields) -> None:
    env["data"]["reviews"].extend(reviews)
    env["data"].update(fields)
    write(env["fixture"], **env["data"])


class Owner:
    """One worker process with its stdout drained on a thread so waits never block."""

    def __init__(self, env, token: str, deadline: float = 5.0) -> None:
        self.token = token
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "tests" / "drain_worker.py"), str(env["db"]),
             token, str(TTL), str(INTERVAL), str(deadline)],
            env=env["env"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
        self.lines: list[str] = []
        self._q: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._drain, daemon=True).start()
        words = self.expect("serving").split()
        self.generation = int(words[words.index("generation") + 1])

    def _drain(self) -> None:
        for line in self.proc.stdout:
            self._q.put(line.rstrip("\n"))

    def expect(self, needle: str, timeout: float = 15.0) -> str:
        for line in self.lines:
            if needle in line:
                return line
        end = time.time() + timeout
        while time.time() < end:
            try:
                line = self._q.get(timeout=0.05)
            except queue.Empty:
                continue
            self.lines.append(line)
            if needle in line:
                return line
        raise AssertionError(f"{self.token} never logged {needle!r}: {self.lines}")

    def finish(self, timeout: float = 15.0) -> int:
        code = self.proc.wait(timeout=timeout)
        if code in (0, 4, 5):
            self.expect(f"exit {code}")             # run_forever returned; not a hard exit
        return code

    def stopped(self) -> bool:
        ps = subprocess.run(["ps", "-o", "stat=", "-p", str(self.proc.pid)],
                            capture_output=True, text=True, check=False)
        return ps.stdout.startswith("T")

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


def spawn(env, owners, token: str, deadline: float = 5.0) -> Owner:
    o = Owner(env, token, deadline)
    owners.append(o)
    return o


def lease(env):
    return query(env, "SELECT owner_token, generation, expires_at, state FROM lease")[0]


def notes(env) -> list[str]:
    return [r["op_key"] for r in query(env, "SELECT op_key FROM notifications")]


def reviews_notified(env) -> list[str]:
    return sorted(k.rsplit(":", 1)[-1] for k in notes(env))


def try_acquire(env, token: str) -> int:
    conn = connect(env["db"])
    try:
        return LeaseManager(conn, "stark", Clock()).acquire(token, TTL)
    finally:
        conn.close()


def settled(env) -> None:
    """Every effect done, every event acked, every notification unique."""
    keys = notes(env)
    assert len(keys) == len(set(keys)), keys
    assert {r["status"] for r in query(env, "SELECT status FROM effects")} <= {"done"}
    assert {r["status"] for r in query(env, "SELECT status FROM events")} <= {"acked"}


def baseline(env, owners, token="host-a", deadline=5.0) -> Owner:
    a = spawn(env, owners, token, deadline)
    wait_for(lambda: query(env, "SELECT 1 FROM monitor_seen WHERE event_id='review:1'"))
    return a


def test_sigterm_idle_drains_releases_and_successor_fences(env, owners):
    a = baseline(env, owners)
    a.proc.send_signal(signal.SIGTERM)
    assert a.finish() == 0, a.lines
    a.expect("draining generation")
    row = lease(env)
    assert (row["owner_token"], row["generation"], row["state"]) == (None, a.generation,
                                                                       "released")
    b = spawn(env, owners, "host-b")
    assert b.generation == a.generation + 1
    assert lease(env)["state"] == "held"


def test_drain_finishes_in_flight_work_and_refuses_successor_until_release(env, owners):
    a = baseline(env, owners)
    put(env, review(2, "approved"), review(3, "changes_requested"), review(4, "approved"),
        notify_delay_s=0.6)                         # about 1.8 s of work, longer than the TTL
    a.expect("notified")
    a.proc.send_signal(signal.SIGTERM)
    a.expect("draining generation")
    put(env, review(5, "changes_requested"))        # arrives after the freeze: not admitted

    samples = []
    while a.proc.poll() is None:
        row = lease(env)
        if row["state"] == "draining":
            samples.append((time.time(), row["expires_at"]))
            with pytest.raises(LeaseDenied):        # the draining owner is still valid
                try_acquire(env, "host-b")
        time.sleep(0.1)
    assert a.finish() == 0, a.lines
    assert samples and samples[-1][0] - samples[0][0] > TTL, samples
    assert all(expires > now for now, expires in samples)   # renewed throughout the drain
    assert samples[-1][1] > samples[0][1]
    assert reviews_notified(env) == ["2", "3", "4"]  # in flight work committed, review 5 not
    assert lease(env)["state"] == "released"

    put(env, notify_delay_s=0)
    b = spawn(env, owners, "host-b")
    assert b.generation == a.generation + 1
    wait_for(lambda: len(notes(env)) == 4)
    time.sleep(0.5)
    assert reviews_notified(env) == ["2", "3", "4", "5"]
    settled(env)


def test_drain_deadline_cancels_and_successor_finishes_exactly_once(env, owners):
    a = baseline(env, owners, deadline=1.0)
    put(env, *(review(i, "approved") for i in range(2, 8)), notify_delay_s=0.8)
    a.expect("notified")
    t0 = time.time()
    a.proc.send_signal(signal.SIGTERM)
    assert a.finish() == 5, a.lines
    assert time.time() - t0 < 1.0 + 2.0 + 1.0       # deadline plus grace, plus process exit
    a.expect("drain deadline reached")
    done_by_a = reviews_notified(env)
    assert 0 < len(done_by_a) < 6
    assert lease(env)["state"] == "released"

    put(env, notify_delay_s=0)
    spawn(env, owners, "host-b")
    wait_for(lambda: len(notes(env)) == 6)
    time.sleep(0.5)
    assert reviews_notified(env) == [str(i) for i in range(2, 8)]
    settled(env)


def test_non_cooperative_stall_aborts_at_grace_and_releases(env, owners):
    a = baseline(env, owners, deadline=0.5)
    put(env, review(2, "approved"), snapshot_delay_s=30)
    time.sleep(0.5)                                 # the next poll is now stuck in the GET
    t0 = time.time()
    a.proc.send_signal(signal.SIGTERM)
    assert a.proc.wait(timeout=10) == 6, a.lines
    assert time.time() - t0 < 0.5 + 2.0 + 1.0
    a.expect("releasing lease and aborting")
    row = lease(env)
    assert (row["owner_token"], row["state"]) == (None, "released")

    put(env, snapshot_delay_s=0)
    b = spawn(env, owners, "host-b")                # no TTL wait: the lease was released
    assert b.generation == a.generation + 1
    wait_for(lambda: len(notes(env)) == 1)
    settled(env)


def test_sigkill_mid_drain_keeps_successor_out_until_expiry(env, owners):
    a = baseline(env, owners)
    put(env, review(2, "approved"), review(3, "approved"), review(4, "approved"),
        notify_delay_s=0.6)
    a.expect("notified")
    a.proc.send_signal(signal.SIGTERM)
    a.expect("draining generation")
    a.proc.kill()
    a.proc.wait(timeout=10)

    assert lease(env)["state"] == "draining"
    with pytest.raises(LeaseDenied):                # dead, but the lease has not expired
        try_acquire(env, "host-b")
    time.sleep(TTL + 0.5)

    put(env, notify_delay_s=0)
    b = spawn(env, owners, "host-b")
    assert b.generation == a.generation + 1
    wait_for(lambda: len(notes(env)) == 3)
    time.sleep(0.5)
    assert reviews_notified(env) == ["2", "3", "4"]
    settled(env)


def test_stale_owner_completion_is_fenced_after_takeover(env, owners):
    a = baseline(env, owners)
    a.proc.send_signal(signal.SIGUSR1)              # stop inside the first notification
    put(env, review(2, "approved"), review(3, "changes_requested"))
    a.expect("pausing")
    wait_for(a.stopped)
    time.sleep(TTL + 0.5)                           # A's lease lapses mid work

    b = spawn(env, owners, "host-b")
    assert b.generation == a.generation + 1
    takeover_at = time.time()
    wait_for(lambda: len(notes(env)) == 2)

    a.proc.send_signal(signal.SIGCONT)              # A resumes and tries to finish
    assert a.finish() == 4, a.lines
    assert any("lost ownership" in line for line in a.lines), a.lines
    stale = query(env, "SELECT key FROM effects WHERE generation=? AND updated>=?",
                  a.generation, takeover_at)
    assert stale == []
    assert reviews_notified(env) == ["2", "3"]
    assert (lease(env)["owner_token"], lease(env)["generation"]) == ("host-b", b.generation)
    settled(env)

    b.proc.send_signal(signal.SIGTERM)
    assert b.finish() == 0, b.lines
