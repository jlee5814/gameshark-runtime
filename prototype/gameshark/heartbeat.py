"""Lease heartbeat and drain deadline enforcement, independent of the work thread.

The work thread may sit inside one long tick (a slow poll, a slow handler). Renewal must
not wait for it, or a drain longer than the TTL would hand ownership to a successor while
in flight work still commits. The heartbeat runs on its own thread with its own SQLite
connection, so it never shares the work thread's connection or transaction.

Drain protocol, driven from here once `stop` is set:

1. Freeze: lifecycle and lease enter draining; admission refuses new work.
2. Drain: keep renewing while in flight work finishes under the still valid generation.
3. Cancel: at the drain deadline, set the cancel token; work stops at its next safe
   boundary and the worker exits 5.
4. Abort: at the deadline plus grace, release the lease (so any late write fails the
   fence) and end the process with exit 6. This bounds a non cooperative stall.

On FencedError the lease is lost: the lifecycle turns LOST and cancels, and the worker's
next fenced write or tick boundary exits 4.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Callable

from gameshark.errors import FencedError
from gameshark.lease import LeaseManager
from gameshark.lifecycle import Phase
from gameshark.store import connect

EXIT_DRAIN_ABORTED = 6
POLL_S = 0.05


class Heartbeat(threading.Thread):
    def __init__(self, sup, stop: threading.Event, drain_deadline_s: float,
                 grace_s: float = 2.0, log: Callable[[str], None] = print,
                 hard_exit: Callable[[int], None] = os._exit) -> None:
        super().__init__(name="lease-heartbeat", daemon=True)
        self.sup = sup
        self.stop_requested = stop
        self.drain_deadline_s = drain_deadline_s
        self.grace_s = grace_s
        self.log = log
        self.hard_exit = hard_exit
        self._done = threading.Event()

    def run(self) -> None:
        sup, life = self.sup, self.sup.lifecycle
        conn = connect(sup.db_path)
        lease = LeaseManager(conn, sup.agent_id, sup.clock)
        next_renew = 0.0
        try:
            while not self._done.is_set():
                now = time.monotonic()
                try:
                    if self.stop_requested.is_set() and life.phase is Phase.SERVING:
                        lease.begin_drain(sup.owner_token, sup.generation)
                        life.begin_drain(now + self.drain_deadline_s)
                        self.log(f"draining generation {sup.generation}; deadline "
                                 f"{self.drain_deadline_s:g}s")
                    if life.phase is Phase.DRAINING and life.deadline is not None:
                        if now >= life.deadline + self.grace_s:
                            self.log("drain grace exhausted; releasing lease and aborting")
                            lease.release(sup.owner_token, sup.generation)
                            self.hard_exit(EXIT_DRAIN_ABORTED)
                            return
                        if now >= life.deadline and not life.token.cancelled:
                            self.log("drain deadline reached; cancelling in flight work")
                            life.token.cancel("drain deadline reached")
                    if now >= next_renew:
                        lease.renew(sup.owner_token, sup.generation, sup.lease_ttl_s)
                        next_renew = now + sup.lease_ttl_s / 3
                except FencedError as e:
                    life.lost(str(e))
                    self.log(f"heartbeat lost ownership: {e}")
                    return
                except sqlite3.Error as e:
                    self.log(f"heartbeat retrying: {type(e).__name__}: {e}")
                self._done.wait(POLL_S)
        finally:
            conn.close()

    def close(self) -> None:
        self._done.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=5)
