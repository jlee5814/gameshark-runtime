"""Subprocess worker: the production daemon serving a live provider.

The same wiring as tests/inference_daemon_worker.py: run_forever with the GitHub monitor
on the file transport and the async assistant. The provider comes from LIVE_* variables
(set by smoke.py from Target.daemon_env) and is wrapped in ObservedProvider, which only
timestamps. Every observation, outcome, and the exit leak check is one stdout line.

Variables beyond Target.daemon_env:
  LIVE_PRIVACY      local_only (default) or cloud_ok, for every conversation
  LIVE_MAX_TOKENS   default max_tokens; LIVE_SPECS is a JSON map topic -> max_tokens
  LIVE_TIMEOUT_S    per call deadline (default 120)
  LIVE_BUDGET       window_s,max_tokens,max_cost_micros applied after start
  LIVE_TEXT=1       record answer text in observations (local synthetic prompts only)
  KILL_AT           crash label (E1 to E4) at which the process SIGKILLs itself

Invoked as: python -m live.daemon <runtime_db> <owner> <ttl_s> <interval_s> <deadline_s>
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

from gameshark.inference import (
    CallSpec,
    Capabilities,
    FakeProvider,
    Locality,
    OpenAICompatProvider,
    Privacy,
    ProviderConfig,
    RouterPolicy,
)
from gameshark.supervisor import AgentSupervisor
from gameshark.worker import run_forever
from live.observed import ObservedProvider
from sims.game import GameSimulator
from sims.github_monitor import GitHubMonitorAdapter
from tests.file_transport import make
from tests.inference_adapter import AsyncAssistantAdapter, call_id_for

_out = threading.Lock()


def log(msg: str) -> None:
    with _out:
        print(f"{time.time():.3f} {msg}", flush=True)


def emit(record: dict) -> None:
    log("obs " + json.dumps(record, separators=(",", ":")))


class KillSupervisor(AgentSupervisor):
    kill_label = ""

    def _maybe_crash(self, label: str) -> None:
        if label == self.kill_label:
            log(f"killing at {label}")
            os.kill(os.getpid(), signal.SIGKILL)


class LiveAssistantAdapter(AsyncAssistantAdapter):
    """Per topic max_tokens and a configured privacy; outcomes logged as JSON."""

    def __init__(self, remote: GameSimulator, privacy: Privacy, default_tokens: int,
                 specs: dict[str, int], timeout_s: float) -> None:
        super().__init__(remote, CallSpec(max_tokens=default_tokens, timeout_s=timeout_s))
        self.privacy = privacy
        self.specs = specs
        self.timeout_s = timeout_s

    def spec_for(self, topic: str) -> CallSpec:
        tokens = self.specs.get(topic)
        return self.spec if tokens is None else CallSpec(max_tokens=tokens,
                                                         timeout_s=self.timeout_s)

    def on_event(self, event: dict):
        log("handle " + json.dumps({"topic": event["payload"]["topic"],
                                    "call_id": call_id_for(event["event_id"])}))
        return super().on_event(event)

    def _answered(self, call_id, cid, rid, result) -> None:
        super()._answered(call_id, cid, rid, result)
        c = result.committed
        log("outcome " + json.dumps({
            "call_id": call_id, "outcome": self.outcomes[-1], "replayed": c.replayed,
            "finish": c.finish_reason and c.finish_reason.value, "provider": result.provider,
            "soft_budget": result.soft_budget, "metered": result.metered}))

    def _failed(self, rid: str, exc: Exception) -> None:
        super()._failed(rid, exc)
        log("outcome " + json.dumps({"rid": rid, "outcome": self.outcomes[-1]}))


def provider(env: dict[str, str]):
    kind = env["LIVE_PROVIDER"]
    slots = int(env["LIVE_SLOTS"])
    if kind == "fake_cloud":
        caps = Capabilities(env["LIVE_MODEL"], Locality.CLOUD, int(env["LIVE_CONTEXT"]),
                            max_concurrency=slots, price_in_micros=int(env["LIVE_PRICE_IN"]),
                            price_out_micros=int(env["LIVE_PRICE_OUT"]))
        return FakeProvider("cloud", caps)
    return OpenAICompatProvider(
        kind, env["LIVE_URL"], env["LIVE_MODEL"], int(env["LIVE_CONTEXT"]),
        max_concurrency=slots, price_in_micros=int(env["LIVE_PRICE_IN"]),
        price_out_micros=int(env["LIVE_PRICE_OUT"]), api_key=env.get("LIVE_API_KEY"),
        read_idle_s=float(env.get("LIVE_TIMEOUT_S", "120")))


def main() -> int:
    db, owner = sys.argv[1], sys.argv[2]
    ttl, interval, deadline = (float(a) for a in sys.argv[3:6])
    env = dict(os.environ)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    inner = provider(env)
    observed = ObservedProvider(inner, emit, record_text=env.get("LIVE_TEXT") == "1"
                                and env["LIVE_PROVIDER"] == "local")
    fingerprint = getattr(inner, "config_fingerprint", "")
    slots = int(env["LIVE_SLOTS"])

    sup = KillSupervisor(db_path=db, agent_id="stark", owner_token=owner, lease_ttl_s=ttl)
    sup.kill_label = env.get("KILL_AT", "")
    sup.add_adapter(GitHubMonitorAdapter(make(), poll_interval_s=interval,
                                         notifier=lambda n: log(f"notified {n['summary']}")))
    timeout_s = float(env.get("LIVE_TIMEOUT_S", "120"))
    sup.add_adapter(LiveAssistantAdapter(
        GameSimulator(Path(db).with_name("game.db")), Privacy(env.get("LIVE_PRIVACY",
                                                                       "local_only")),
        int(env.get("LIVE_MAX_TOKENS", "64")), json.loads(env.get("LIVE_SPECS", "{}")),
        timeout_s))
    sup.enable_inference([ProviderConfig(observed, slots=slots, fingerprint=fingerprint)],
                         policy=RouterPolicy(agent_in_flight=2 * slots,
                                             conversation_in_flight=1),
                         retry_s=0.2)
    sup.start()
    if env.get("LIVE_BUDGET"):
        window, tokens, micros = env["LIVE_BUDGET"].split(",")
        sup.inference.set_budget(float(window), int(tokens), int(micros))
    log(f"serving {owner} generation {sup.generation} uuid {sup.identity.uuid}")
    code = run_forever(sup, stop, interval, log=log, drain_deadline_s=deadline)
    ex = sup.executor.stats()
    log(f"leaks tasks={ex.tasks} jobs={ex.jobs_queued} completions={ex.completions_queued} "
        f"claims={len(sup.router.claimed())} reserved_live={len(sup.inference._active)} "
        f"threads={ex.threads_alive} refused={ex.refused} dispatched={observed.dispatched} "
        f"peak_active={observed.peak}")
    log(f"exit {code}")
    sup.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
