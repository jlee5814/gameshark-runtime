"""Subprocess worker: the real run_forever daemon with GitHub monitoring and async inference.

The GitHub monitor reads the file transport named by GAMESHARK_FIXTURE. The assistant
answers ask controls through the inference executor, on either:

- INFERENCE_URL set: an OpenAICompatProvider aimed at a loopback fixture the parent owns.
- otherwise: a FakeProvider whose call for topic tN waits on the file INFERENCE_GATES/open-N
  (or never waits with INFERENCE_GATES=open). The wait honors cancellation unless
  INFERENCE_DEAF is set, which models a provider that ignores it.

KILL_AT names a crash label (E1 to E4) at which the process sends itself SIGKILL. SIGTERM
starts a graceful drain; the exit code is run_forever's. Each owner token models a host.

Invoked as: python inference_daemon_worker.py <runtime_db> <owner> <ttl_s> <interval_s>
            <deadline_s>
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.inference import (
    Capabilities,
    FakeProvider,
    Locality,
    OpenAICompatProvider,
    ProviderConfig,
    RouterPolicy,
    Script,
)
from gameshark.supervisor import AgentSupervisor
from gameshark.worker import run_forever
from sims.game import GameSimulator
from sims.github_monitor import GitHubMonitorAdapter
from tests.file_transport import make
from tests.inference_adapter import AsyncAssistantAdapter, ask_event, call_id_for

TOPICS = 8


def log(msg: str) -> None:
    print(f"{time.time():.3f} {msg}", flush=True)


class KillSupervisor(AgentSupervisor):
    kill_label = ""

    def _maybe_crash(self, label: str) -> None:
        if label == self.kill_label:
            log(f"killing at {label}")
            os.kill(os.getpid(), signal.SIGKILL)


def gate_sleep(gates: str, deaf: bool):
    def sleep(seconds: float, token) -> None:
        if gates == "open":
            return
        gate = Path(gates) / f"open-{int(seconds)}"
        log(f"provider waiting on {gate.name}")
        while not gate.exists():
            if token.cancelled and not deaf:
                return
            time.sleep(0.01)

    return sleep


def provider():
    url = os.environ.get("INFERENCE_URL")
    if url:
        return OpenAICompatProvider("local", url, "m", 4096, max_concurrency=2)
    # Topic tN sleeps N "seconds", which the gate maps to the file open-N.
    scripts = {call_id_for(ask_event(f"t{n}")["event_id"]): Script(delays=(float(n),))
               for n in range(1, TOPICS + 1)}
    return FakeProvider("local", Capabilities("local-model", Locality.LOCAL, 4096,
                                              max_concurrency=2), scripts=scripts,
                        sleep=gate_sleep(os.environ.get("INFERENCE_GATES", "open"),
                                         bool(os.environ.get("INFERENCE_DEAF"))))


def main() -> int:
    db, owner = sys.argv[1], sys.argv[2]
    ttl, interval, deadline = (float(a) for a in sys.argv[3:6])
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    sup = KillSupervisor(db_path=db, agent_id="stark", owner_token=owner, lease_ttl_s=ttl)
    sup.kill_label = os.environ.get("KILL_AT", "")
    sup.add_adapter(GitHubMonitorAdapter(make(), poll_interval_s=interval,
                                         notifier=lambda n: log(f"notified {n['summary']}")))
    sup.add_adapter(AsyncAssistantAdapter(GameSimulator(Path(db).with_name("game.db"))))
    sup.enable_inference([ProviderConfig(provider())],
                         policy=RouterPolicy(agent_in_flight=8, conversation_in_flight=1),
                         retry_s=0.2)
    sup.start()
    log(f"serving {owner} generation {sup.generation} uuid {sup.identity.uuid}")
    code = run_forever(sup, stop, interval, log=log, drain_deadline_s=deadline)
    ex = sup.executor.stats()
    log(f"leaks tasks={ex.tasks} jobs={ex.jobs_queued} completions={ex.completions_queued} "
        f"claims={len(sup.router.claimed())} reserved_live={len(sup.inference._active)} "
        f"peak_completions={ex.peak_completions} threads={ex.threads_alive}")
    log(f"exit {code}")
    sup.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
