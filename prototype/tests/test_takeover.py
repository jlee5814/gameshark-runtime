"""Acceptance: real processes contend for Stark through the lease alone.

Owner A stalls (SIGSTOP between ticks) past its TTL, owner B takes over and serves, then
A resumes. A must detect the takeover on its next renewal and exit 4 without committing.
B then dies by SIGKILL while activity arrives, and owner C recovers it exactly once. Each
owner is a separate `takeover_worker.py` process running the real run_forever loop.
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

from tests.file_transport import review, write
from tests.test_daemon import ROOT, query, stark, wait_for, wait_ticks

TTL = 1.0
INTERVAL = 0.05

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


@pytest.fixture
def env(tmp_path):
    fixture = tmp_path / "github.json"
    e = {**os.environ, "GAMESHARK_TRANSPORT": "tests.file_transport:make",
         "GAMESHARK_FIXTURE": str(fixture), "PYTHONPATH": str(ROOT)}
    e.pop("GITHUB_TOKEN", None)
    return {"env": e, "db": tmp_path / "runtime.db", "fixture": fixture,
            "reviews": [review(1, "approved", author="old", ago_s=86400)]}


def publish(env, *reviews) -> None:
    env["reviews"].extend(reviews)
    write(env["fixture"], reviews=env["reviews"])


class Owner:
    """One worker process with its stdout drained on a thread so waits never block."""

    def __init__(self, env, token: str) -> None:
        self.token = token
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "tests" / "takeover_worker.py"), str(env["db"]),
             token, str(TTL), str(INTERVAL)],
            env=env["env"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
        self.lines: list[str] = []
        self._q: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._drain, daemon=True).start()
        self.generation, self.uuid = self._serving()

    def _drain(self) -> None:
        for line in self.proc.stdout:
            self._q.put(line.rstrip("\n"))

    def expect(self, needle: str, timeout: float = 15.0) -> str:
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

    def _serving(self) -> tuple[int, str]:
        words = self.expect("serving").split()
        return int(words[words.index("generation") + 1]), words[words.index("uuid") + 1]

    def pause(self) -> None:
        self.proc.send_signal(signal.SIGUSR1)
        self.expect("pausing")
        wait_for(self.stopped)

    def stopped(self) -> bool:
        ps = subprocess.run(["ps", "-o", "stat=", "-p", str(self.proc.pid)],
                            capture_output=True, text=True, check=False)
        return ps.stdout.startswith("T")

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)


def lease(env):
    return query(env, "SELECT owner_token, generation FROM lease")[0]


def notes(env):
    return [r["op_key"] for r in query(env, "SELECT op_key FROM notifications")]


def test_stale_owner_exits_and_successor_recovers_after_sigkill(env):
    publish(env)
    assert stark(env, "watch", "acme/widget#42").returncode == 0
    owners: list[Owner] = []
    try:
        a = Owner(env, "host-a")
        owners.append(a)
        a.pause()
        time.sleep(TTL + 0.5)                       # A's lease lapses while it is stalled

        b = Owner(env, "host-b")
        owners.append(b)
        assert b.generation == a.generation + 1 and b.uuid == a.uuid
        takeover_at = time.time()
        publish(env, review(2, "changes_requested"))
        wait_for(lambda: len(notes(env)) == 1)      # B serves under its own fence

        a.proc.send_signal(signal.SIGCONT)
        assert a.proc.wait(timeout=10) == 4, a.lines
        a.expect("exit 4")
        assert any("lost ownership" in line for line in a.lines), a.lines
        assert not any("tick failed" in line for line in a.lines), a.lines

        assert tuple(lease(env)) == ("host-b", b.generation)
        stale = query(env, "SELECT key FROM effects WHERE generation=? AND updated>=?",
                      a.generation, takeover_at)
        assert stale == []
        assert [r["generation"] for r in query(env, "SELECT generation FROM effects")] == [
            b.generation]

        b.kill()                                    # SIGKILL mid serve, lease still live
        publish(env, review(3, "approved"))         # lands while nobody serves
        time.sleep(TTL + 0.5)

        c = Owner(env, "host-c")
        owners.append(c)
        assert c.generation == b.generation + 1 and c.uuid == a.uuid
        wait_for(lambda: len(notes(env)) == 2)
        wait_ticks(env, 5)                          # C keeps polling; nothing repeats

        keys = notes(env)
        assert len(keys) == len(set(keys)) == 2
        assert any("review:2" in k for k in keys) and any("review:3" in k for k in keys)
        assert not any("review:1" in k for k in keys)   # baseline history stays silent
        assert tuple(lease(env)) == ("host-c", c.generation)

        c.proc.send_signal(signal.SIGTERM)
        assert c.proc.wait(timeout=10) == 0
    finally:
        for o in owners:
            if o.proc.poll() is None:
                o.proc.send_signal(signal.SIGCONT)
            o.kill()
