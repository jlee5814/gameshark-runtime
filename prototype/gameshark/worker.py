"""Execution worker: keeps one supervisor serving its responsibilities without prompts.

Layering: identity and ownership live in the supervisor; responsibilities live in
SQLite; adapters touch the environment. The worker only schedules: renew the lease,
apply queued operator requests, poll, sleep until the earliest due responsibility. It
holds no durable truth of its own, so killing it at any instruction loses nothing.

Operator requests (watch, unwatch, resume) arrive through `control_requests`, a table any
process may append to without a lease. Only the lease holder applies them, so a CLI never
races the daemon for ownership.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Self

from gameshark.errors import Cancelled, FencedError
from gameshark.heartbeat import Heartbeat
from gameshark.lifecycle import Phase
from gameshark.supervisor import AgentSupervisor


class AlreadyRunning(Exception):
    """Another process on this host holds the runtime lock."""


class InstanceLock:
    """One serving process per database on a host, released by the kernel on death.

    The lease fences writers across hosts; this lock lets a restarted daemon on the same
    host reclaim its own lease at once instead of waiting out the TTL after a SIGKILL.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.path = f"{db_path}.lock"
        self.fd: int | None = None

    def acquire(self) -> Self:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            os.close(fd)
            raise AlreadyRunning(f"another Stark process holds {self.path}") from e
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self.fd = fd
        return self

    def release(self) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None

    def held_elsewhere(self) -> bool:
        try:
            self.acquire()
        except AlreadyRunning:
            return True
        self.release()
        return False

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


def enqueue_control(conn, agent_id: str, adapter: str, action: str, payload: dict) -> int:
    cur = conn.execute(
        "INSERT INTO control_requests(agent_id, adapter, action, payload, status, created) "
        "VALUES(?,?,?,?, 'pending', ?)",
        (agent_id, adapter, action, json.dumps(payload), time.time()),
    )
    return int(cur.lastrowid)


def apply_controls(sup: AgentSupervisor) -> list[str]:
    """Apply pending operator requests in order. Each action is idempotent, so a crash
    after applying and before marking replays harmlessly."""
    rows = sup.conn.execute(
        "SELECT * FROM control_requests WHERE agent_id=? AND status='pending' ORDER BY id",
        (sup.agent_id,),
    ).fetchall()
    out = []
    for r in rows:
        try:
            result = sup.registry.get(r["adapter"]).apply_control(r["action"],
                                                                 json.loads(r["payload"]))
            status, detail = "applied", str(result)
        except (KeyError, ValueError) as e:
            status, detail = "rejected", str(e)

        def _mark(conn, rid=r["id"], status=status, detail=detail):
            conn.execute("UPDATE control_requests SET status=?, detail=?, applied=? WHERE id=?",
                         (status, detail, time.time(), rid))

        sup._fenced(_mark)
        out.append(f"{r['action']}:{status}:{detail}")
    return out


def next_due(report: dict) -> float | None:
    dues = [r["next_due"] for r in report.values()
            if isinstance(r, dict) and r.get("next_due") is not None]
    return min(dues) if dues else None


EXIT_LOST = 4
EXIT_DRAIN_CANCELLED = 5


def run_forever(sup: AgentSupervisor, stop: threading.Event, interval_s: float,
                log=print, max_ticks: int | None = None, drain_deadline_s: float = 10.0,
                hard_exit=None) -> int:
    """Serve until stop is set, then drain and release. Return a process exit code.

    Unexpected errors in one tick are logged and retried next tick. Exit codes:

    - 0: drained cleanly; in flight work finished and the lease was released.
    - 4: lost ownership. A renewal or fenced write raised FencedError: another owner holds
      a newer generation, or ours expired or was released, so retrying can never succeed.
    - 5: the drain deadline cancelled in flight work at a safe boundary; it stays durable
      for the successor.
    - 6: the drain grace ran out on non cooperative work; the heartbeat released the lease
      and ended the process (see heartbeat.py).

    A heartbeat thread renews the lease throughout, so a tick longer than the TTL, or a
    drain, never lets a successor in while this owner still commits.
    """
    beat = Heartbeat(sup, stop, drain_deadline_s, log=log,
                     **({"hard_exit": hard_exit} if hard_exit else {}))
    beat.start()
    ticks = 0
    code = 0
    try:
        while not stop.is_set():
            if sup.lifecycle.phase is Phase.LOST:
                log(f"lost ownership, exiting: {sup.lifecycle.token.reason}")
                return EXIT_LOST
            try:
                sup.lease.renew(sup.owner_token, sup.generation, sup.lease_ttl_s)
                for line in apply_controls(sup):
                    log(f"control {line}")
                report = sup.poll()
            except FencedError as e:
                log(f"lost ownership, exiting: {e}")
                return EXIT_LOST
            except Cancelled as e:
                if sup.lifecycle.phase is Phase.LOST:
                    log(f"lost ownership, exiting: {e}")
                    return EXIT_LOST
                log(f"drain cancelled in flight work: {e}")
                code = EXIT_DRAIN_CANCELLED
                break
            except Exception as e:
                log(f"tick failed, retrying: {type(e).__name__}: {e}")
                report = {}
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            due = next_due(report)
            now = sup.clock.now()
            wait = interval_s if due is None else max(0.0, min(due - now, interval_s))
            # Renew well inside the TTL even when nothing is due for a while.
            stop.wait(min(max(wait, 0.05), sup.lease_ttl_s / 3))
        if stop.is_set():
            # The heartbeat records draining; wait briefly so every stop passes through it.
            end = time.monotonic() + 1.0
            while sup.lifecycle.phase is Phase.SERVING and time.monotonic() < end:
                time.sleep(0.01)
            if sup.lifecycle.phase is Phase.LOST:
                log(f"lost ownership, exiting: {sup.lifecycle.token.reason}")
                return EXIT_LOST
            log(f"drained; releasing generation {sup.generation}")
        return code
    finally:
        beat.close()
        sup.stop()
        sys.stdout.flush()
