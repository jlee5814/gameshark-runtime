"""Subprocess worker for the takeover test: the real run_forever loop under one owner token.

Each instance models a distinct host, so two of them contend through the lease alone (no
instance lock). SIGUSR1 asks the worker to stop itself at the idle point between ticks,
never while it holds the SQLite write lock; the parent resumes it with SIGCONT. SIGTERM
stops it cleanly. The process exit code is run_forever's.

Invoked as: python takeover_worker.py <runtime_db> <owner_token> <ttl_s> <interval_s>
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.supervisor import AgentSupervisor
from gameshark.worker import run_forever
from sims.github_monitor import GitHubMonitorAdapter
from tests.file_transport import make


class PausableStop(threading.Event):
    pause_requested = False

    def wait(self, timeout: float | None = None) -> bool:
        if self.pause_requested:
            self.pause_requested = False
            log("pausing")
            os.kill(os.getpid(), signal.SIGSTOP)
            log("resumed")
        return super().wait(timeout)


def log(msg: str) -> None:
    print(f"{time.time():.3f} {msg}", flush=True)


def main() -> int:
    db, owner, ttl, interval = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
    stop = PausableStop()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGUSR1, lambda *_: setattr(stop, "pause_requested", True))

    sup = AgentSupervisor(db_path=db, agent_id="stark", owner_token=owner, lease_ttl_s=ttl)
    sup.add_adapter(GitHubMonitorAdapter(make(), poll_interval_s=interval))
    sup.start()
    log(f"serving {owner} generation {sup.generation} uuid {sup.identity.uuid}")
    code = run_forever(sup, stop, interval, log=log)
    log(f"exit {code}")
    sup.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
