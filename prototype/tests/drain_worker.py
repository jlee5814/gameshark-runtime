"""Subprocess worker for the drain tests: the real run_forever loop under one owner token.

Each instance models a distinct host, so owners contend through the lease alone (no
instance lock). SIGTERM starts a graceful drain. The notifier runs inside in flight work,
right after a notification commits and before its effect records done: it sleeps for the
fixture's notify_delay_s to make that work slow, and after SIGUSR1 it stops the whole
process there (SIGSTOP) so the parent can let the lease lapse mid work. The process exit
code is run_forever's.

Invoked as: python drain_worker.py <runtime_db> <owner_token> <ttl_s> <interval_s> <deadline_s>
"""

from __future__ import annotations

import json
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


def log(msg: str) -> None:
    print(f"{time.time():.3f} {msg}", flush=True)


def main() -> int:
    db, owner = sys.argv[1], sys.argv[2]
    ttl, interval, deadline = (float(a) for a in sys.argv[3:6])
    fixture = Path(os.environ["GAMESHARK_FIXTURE"])
    stop, pause = threading.Event(), threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGUSR1, lambda *_: pause.set())

    def notifier(note: dict) -> None:
        log(f"notified {note['kind']} {note['summary']}")
        if pause.is_set():
            pause.clear()
            log("pausing")
            os.kill(os.getpid(), signal.SIGSTOP)
            log("resumed")
        time.sleep(json.loads(fixture.read_text(encoding="utf8")).get("notify_delay_s", 0))

    sup = AgentSupervisor(db_path=db, agent_id="stark", owner_token=owner, lease_ttl_s=ttl)
    sup.add_adapter(GitHubMonitorAdapter(make(), poll_interval_s=interval, notifier=notifier))
    sup.start()
    log(f"serving {owner} generation {sup.generation} uuid {sup.identity.uuid}")
    code = run_forever(sup, stop, interval, log=log, drain_deadline_s=deadline)
    log(f"exit {code}")
    sup.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
