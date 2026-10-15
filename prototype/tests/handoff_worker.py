"""Subprocess worker that SIGKILLs itself mid jack.

It takes the owner lease on the real clock, places presence at home on the mac, opens a
jack to the phone, and dies at one labeled crash point with no cleanup. The parent then
recovers from the database alone.

Invoked as: python handoff_worker.py <runtime_db> <kill_label>
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.clock import Clock
from gameshark.lease import LeaseManager
from gameshark.presence import PresenceManager
from gameshark.store import connect
from tests.helpers import AGENT, HOST


def main() -> None:
    runtime_db, kill_label = sys.argv[1:3]

    def crash(label: str) -> None:
        if label == kill_label:
            sys.stdout.flush()
            os.kill(os.getpid(), signal.SIGKILL)

    conn = connect(runtime_db)
    clock = Clock()
    lease = LeaseManager(conn, AGENT, clock)
    gen = lease.acquire(HOST, 30.0)
    presence = PresenceManager(conn, AGENT, lease, gen, clock, crash=crash)
    presence.ensure_home("mac")
    presence.request_jack("h1", "phone")      # P1_after_prepare fires here
    presence.ready_out("h1", "mac", 1, draft="half typed")  # P2_after_commit fires here
    sys.exit(99)                               # the kill did not fire


if __name__ == "__main__":
    main()
