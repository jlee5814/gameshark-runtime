"""AgentSupervisor: wires the primitives into a recoverable runtime.

A supervisor instance models one process lifetime. A crash discards the instance; a
fresh instance on the same database models a restart. Nothing in memory survives, so
recovery is a pure function of durable SQLite state.

With enable_inference, an event handler may return an InferenceTask instead of finishing
inline. The supervisor submits it to a bounded executor and leaves the event pending and
claimed; the event is acknowledged only after the task's continuation ran on this thread,
which owns the connection. Retryable refusals release the event for redelivery with the
same call_id; ownership loss drops the task without any write.

With enable_presence, start() also builds the generation bound PresenceManager and
SurfaceConversation, so a surface gateway can fence its writes by presence generation.

start() always builds the MemoryStore on the same connection and lease: saved memories
reach inference through the journal, and their writes come only from typed surface
commands and operator controls (memory.py).
"""

from __future__ import annotations

import dataclasses
import json
import math
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from gameshark.activity import ActivityTracker
from gameshark.adapters import Adapter, AdapterContext, AdapterRegistry
from gameshark.authz import GrantStore
from gameshark.clock import Clock
from gameshark.conversation import SurfaceConversation
from gameshark.effects import Effect, EffectJournal
from gameshark.errors import CrashInjected, Draining, UncommittedOutput
from gameshark.events import Deferred, EventRouter
from gameshark.identity import Identity, load_or_create_identity
from gameshark.inference.executor import InferenceExecutor, InferenceTask
from gameshark.inference.journal import InferenceJournal
from gameshark.inference.router import (
    InferenceRouter,
    ProviderConfig,
    ProviderHealth,
    RouteResult,
    RouterPolicy,
)
from gameshark.inference.scheduler import Scheduler
from gameshark.inference.tracker import ModelTracker, TrackerPolicy
from gameshark.inference.types import ProviderCancelled, ProviderError
from gameshark.lease import LeaseManager
from gameshark.lifecycle import Lifecycle, Phase
from gameshark.memory import MemoryStore
from gameshark.presence import PresenceManager
from gameshark.responsibilities import ResponsibilityStore
from gameshark.store import connect, write_txn


class AgentSupervisor:
    def __init__(
        self,
        db_path: str | Path,
        agent_id: str,
        owner_token: str,
        clock: Clock | None = None,
        crash_at: set[str] | None = None,
        lease_ttl_s: float = 30.0,
    ) -> None:
        self.db_path = str(db_path)
        self.agent_id = agent_id
        self.owner_token = owner_token
        self.clock = clock or Clock()
        self.crash_at = set(crash_at or ())
        self.lease_ttl_s = lease_ttl_s

        self.conn = connect(self.db_path)
        self.identity: Identity = load_or_create_identity(self.conn, agent_id, self.clock)
        self.lease = LeaseManager(self.conn, agent_id, self.clock)
        self.grants = GrantStore(self.conn, agent_id, self.clock)
        self.generation: int | None = None
        self.registry = AdapterRegistry()
        self.responsibilities: ResponsibilityStore | None = None
        self.journal: EffectJournal | None = None
        self.inference: InferenceJournal | None = None
        self.router: EventRouter | None = None
        self._declared: list[Adapter] = []
        self.started = False
        self.lifecycle = Lifecycle()
        self._inference_cfg: dict | None = None
        self.tracker: ModelTracker | None = None
        self.inference_router: InferenceRouter | None = None
        self.executor: InferenceExecutor | None = None
        self.drain_cancelled = 0  # calls the drain deadline cancelled; their events wait
        # One wake for the coordinator: inference completions and gateway requests set it.
        self.wake = threading.Event()
        self._presence_home: str | None = None
        self._presence_timeouts: dict[str, float] = {}
        self.presence: PresenceManager | None = None
        self.surface: SurfaceConversation | None = None
        self.memory: MemoryStore | None = None
        # What runs right now, for surfaces. In memory and read only: never durable state.
        self.activity = ActivityTracker()

    # ------------------------------------------------------------------ crash hook
    def _maybe_crash(self, label: str) -> None:
        if label in self.crash_at:
            self.crash_at.discard(label)      # fire once so recovery can proceed
            raise CrashInjected(label)

    # ------------------------------------------------------------------ setup
    def add_adapter(self, adapter: Adapter) -> None:
        """Declare an adapter. Its declared caps are recorded durably at start()."""
        self._declared.append(adapter)

    def enable_inference(self, providers: Sequence[ProviderConfig],
                         policy: RouterPolicy | None = None,
                         tracker_policy: TrackerPolicy | None = None,
                         threads: int | None = None, completion_depth: int | None = None,
                         retry_s: float = 1.0) -> None:
        """Serve InferenceTasks returned by event handlers on a bounded executor.

        Call before start(). retry_s is the least delay before a refused or retryable
        task's event is redelivered.
        """
        self._inference_cfg = {"providers": list(providers), "policy": policy,
                               "tracker_policy": tracker_policy, "threads": threads,
                               "completion_depth": completion_depth, "retry_s": retry_s}

    def enable_presence(self, home: str, **timeouts: float) -> None:
        """Track Stark's active surface, placed at home on first start. Call before start().

        timeouts may set prepare_timeout_s and ack_timeout_s for the jack deadlines.
        """
        self._presence_home = home
        self._presence_timeouts = timeouts

    def _start_inference(self) -> None:
        cfg = self._inference_cfg
        assert cfg is not None and self.inference is not None
        self.tracker = ModelTracker(Scheduler(), ProviderHealth(), cfg["tracker_policy"])
        self.inference_router = InferenceRouter(self.inference, cfg["providers"],
                                                cfg["policy"], tracker=self.tracker,
                                                grants=self.grants)
        self.executor = InferenceExecutor(
            self.inference_router, self.lifecycle.token, threads=cfg["threads"],
            completion_depth=cfg["completion_depth"], crash=self._maybe_crash,
            admit=self._admit_inference, wake=self.wake)
        self.activity.calls = self.executor.calls

    def _admit_inference(self) -> None:
        # Recovery replays before serving, so starting admits too; draining does not.
        if self.lifecycle.phase not in (Phase.STARTING, Phase.SERVING):
            raise Draining(f"not starting inference while {self.lifecycle.phase.value}")

    def _emit_for(self, adapter_name: str):
        """Return a gated emit_effect bound to one adapter.

        Authorization is checked against durable grants at dispatch time, so a mid
        session or pre restart revocation is honored.
        """
        assert self.journal is not None

        def emit(eff: Effect) -> str:
            if not self.grants.effective(adapter_name, eff.required_cap):
                raise PermissionError(
                    f"{adapter_name} not authorized for {eff.required_cap}"
                )
            # R5: an effect built from model output cites its call, which must have committed.
            call_id = eff.intent.get("call_id")
            if call_id is not None and not self.inference.is_done(call_id):
                raise UncommittedOutput(f"effect {eff.key} cites uncommitted call {call_id}")
            return self.journal.perform_once(eff)

        return emit

    def _make_ctx(self, adapter_name: str) -> AdapterContext:
        return AdapterContext(
            agent_id=self.agent_id,
            identity_uuid=self.identity.uuid,
            caps=self.grants.effective_caps(adapter_name),
            emit_effect=self._emit_for(adapter_name),
            responsibilities=self.responsibilities,
            submit_event=self._submit_inflight,
            conn=self.conn,
            grants=self.grants,
            fenced=self._fenced,
            generation=self.generation or 0,
            clock=self.clock,
            admit=self.lifecycle.admit,
            cancel=self.lifecycle.token,
            inference=self.inference,
            presence=self.presence,
            activity=self.activity,
            memory=self.memory,
        )

    def _fenced(self, fn):
        """Run fn(conn) inside one lease guarded transaction and return its result.

        Every adapter write goes through here, so a stale owner's write raises FencedError
        and commits nothing.
        """
        with write_txn(self.conn):
            self.lease.guard_in_txn(self.generation)
            return fn(self.conn)

    def start(self) -> None:
        """Acquire ownership, build generation bound stores, open adapters, recover."""
        self.generation = self.lease.acquire(self.owner_token, self.lease_ttl_s)
        self.responsibilities = ResponsibilityStore(
            self.conn, self.agent_id, self.lease, self.generation, self.clock
        )
        self.journal = EffectJournal(
            self.conn, self.agent_id, self.lease, self.generation, self.clock,
            crash=self._maybe_crash,
        )
        self.inference = InferenceJournal(
            self.conn, self.agent_id, self.lease, self.generation, self.clock,
            crash=self._maybe_crash,
        )
        if self._presence_home is not None:
            self.presence = PresenceManager(self.conn, self.agent_id, self.lease,
                                            self.generation, self.clock,
                                            crash=self._maybe_crash,
                                            **self._presence_timeouts)
            self.presence.ensure_home(self._presence_home)
            self.surface = SurfaceConversation(self.conn, self.agent_id, self.lease,
                                               self.generation, self.presence, self.clock,
                                               crash=self._maybe_crash)
        self.memory = MemoryStore(self.conn, self.agent_id, self.lease, self.generation,
                                  self.presence, self.grants, self.clock,
                                  crash=self._maybe_crash)
        self.memory.ensure_grants()
        # A forget committed just before a crash may still sit in the WAL; no resend is owed.
        self.memory.scrub()
        self.inference.memory = self.memory
        if self._inference_cfg is not None:
            self._start_inference()
        self.router = EventRouter(
            self.conn, self.lease, self.generation, self.clock, dispatch=self._dispatch,
            cancel=self.lifecycle.token,
        )
        for adapter in self._declared:
            self.registry.register(adapter)
            # Record declared caps without overriding a prior revocation (durable authz).
            self.grants.ensure_granted(adapter.name, adapter.required_caps)
        for name in self.registry.names():
            self.registry.open(name, self._make_ctx)
        self.started = True
        self.recover()
        self.lifecycle.serve()

    def revoke(self, adapter: str, cap: str) -> None:
        """Durably revoke a capability. It stays revoked across restarts."""
        self.grants.revoke(adapter, cap)

    # ------------------------------------------------------------------ routing
    def _dispatch(self, event: dict) -> Deferred | None:
        adapter = self.registry.get(event["adapter"])
        out = adapter.on_event(event)
        if isinstance(out, InferenceTask):
            return self._submit_task(event["event_id"], out)
        if isinstance(out, Deferred):
            return out                       # the handler leaves the event for a later owner
        return None

    # ------------------------------------------------------------------ inference
    def _submit_task(self, event_id: str, task: InferenceTask) -> Deferred | None:
        """Start an event's inference call. None: its outcome ran inline, so the event acks
        now. Deferred: the event stays pending, claimed until the call settles, or released
        for redelivery after a retryable refusal."""
        if self.executor is None:
            raise RuntimeError("a handler returned an InferenceTask; call enable_inference")
        assert self.router is not None
        router = self.router
        wrapped = dataclasses.replace(
            task,
            on_done=lambda r: router.finish(event_id, lambda: task.on_done(r)),
            on_error=lambda e: self._task_failed(event_id, task, e))
        try:
            done = self.executor.submit(wrapped, on_abort=lambda: router.release(
                event_id, time.monotonic() + self._inference_cfg["retry_s"]))
        except Draining:
            return Deferred(math.inf)        # the next owner replays it
        except ProviderError as exc:
            delay = self._retry_delay(exc)
            if delay is None:
                task.on_error(exc)
                return None
            return Deferred(self._retry_at(task, exc, delay))
        if isinstance(done, RouteResult):
            task.on_done(done)
            return None
        return Deferred()

    def _task_failed(self, event_id: str, task: InferenceTask, exc: ProviderError) -> None:
        assert self.router is not None
        delay = self._retry_delay(exc)
        if delay is None:
            self.router.finish(event_id, lambda: task.on_error(exc))
        else:
            self.router.release(event_id, self._retry_at(task, exc, delay))

    @staticmethod
    def _retry_at(task: InferenceTask, exc: ProviderError, delay: float) -> float:
        """When the event retries; the task's owner hears why it waits."""
        at = time.monotonic() + delay
        if task.on_retry is not None:
            task.on_retry(exc, at)
        return at

    def _retry_delay(self, exc: ProviderError) -> float | None:
        """Seconds until the owning event retries, or None when the error is its outcome."""
        if isinstance(exc, ProviderCancelled) and self.lifecycle.token.cancelled:
            self.drain_cancelled += 1
            return math.inf                  # drain or ownership loss: the successor replays
        if not exc.retryable:
            return None
        assert self._inference_cfg is not None
        return max(getattr(exc, "retry_after", 0.0), self._inference_cfg["retry_s"])

    def pump_inference(self) -> int:
        """Advance inference calls whose executor work finished. Coordinator thread only."""
        return self.executor.pump() if self.executor is not None else 0

    def redeliver(self) -> list[str]:
        """Hand released events back to their handlers once their retry time came."""
        if self.router is None or self.lifecycle.phase is not Phase.SERVING:
            return []
        return self.router.redeliver_due()

    def abandon_inference(self, write: bool = True) -> int:
        """Drop every call in flight; their events stay pending for replay. write settles
        held reservations lost and requires the lease; pass False once ownership is lost."""
        if self.executor is None:
            return 0
        dropped = self.executor.abandon_all(write)
        assert self.router is not None
        self.router.release_all()
        return dropped

    def submit(self, event: dict) -> str:
        """Accept a new external event durably and handle it.

        New work: raises Draining once the owner stops admitting, before anything persists,
        so the event stays with its source for the successor.
        """
        assert self.router is not None
        self.lifecycle.admit()
        return self.router.deliver(event)

    def _submit_inflight(self, event: dict) -> str:
        """Submit an event detected by work already admitted (an adapter's poll).

        In flight work may finish while draining; only the drain deadline stops it.
        """
        assert self.router is not None
        self.lifecycle.token.check()
        return self.router.deliver(event)

    def poll(self) -> dict:
        """Ask each adapter that supports polling to detect and feed new events.

        A detecting adapter calls ctx.submit_event for each change, so detection flows
        through the same durable inbox as any other event. Returns a per adapter report.
        """
        report: dict[str, object] = {}
        for name in self.registry.names():
            adapter = self.registry.get(name)
            poll_fn = getattr(adapter, "poll", None)
            if poll_fn is not None:
                report[name] = poll_fn(self._make_ctx(name))
        return report

    # ------------------------------------------------------------------ recovery
    def recover(self) -> dict:
        """Settle abandoned inference reservations, reconcile pending effects, then replay
        unacknowledged events.

        Pure function of durable state. Inference calls are never driven from here: the
        replayed event or responsibility that owns a call reissues it with the same call_id.
        Returns a small report for observability.
        """
        assert self.journal is not None and self.router is not None
        assert self.inference is not None
        lost = self.inference.settle_abandoned()
        reconciled = self._reconcile_pending_effects()
        replayed = self.router.replay()
        return {"lost_reservations": [f"{c}#{a}" for c, a in lost],
                "reconciled_effects": reconciled, "replayed_events": replayed}

    def _reconcile_pending_effects(self) -> list[str]:
        assert self.journal is not None and self.responsibilities is not None
        outcomes: list[str] = []
        for row in self.journal.pending():
            intent = json.loads(row["intent"])
            adapter = self.registry.get(intent["adapter"])
            eff: Effect = adapter.build_effect(intent)
            external_id = self.journal.reconcile(eff)
            rid = intent.get("responsibility_id")
            if external_id is None:
                # Blocked and unreconcilable: escalate the owning responsibility now.
                # The event replay below cannot complete it, so the terminal state is set here.
                if rid is not None:
                    self.responsibilities.transition(rid, "blocked", checkpoint="ambiguous")
                outcomes.append(f"{row['key']}:blocked")
            else:
                # Completed on recovery. The owning responsibility's terminal state is set by
                # the event replay that follows, which re-runs the adapter handler idempotently.
                outcomes.append(f"{row['key']}:done")
        return outcomes

    # ------------------------------------------------------------------ teardown
    def stop(self) -> None:
        if not self.started:
            return
        if self.executor is not None:
            # A lost owner writes nothing more; the successor's recovery settles the rest.
            self.abandon_inference(write=self.lifecycle.phase is not Phase.LOST)
            self.executor.close(timeout_s=0.2)
            assert self.tracker is not None
            self.tracker.close()
        self.registry.teardown_all()
        assert self.generation is not None
        self.lease.release(self.owner_token, self.generation)
        self.started = False
        self.lifecycle.stopped()

    def close(self) -> None:
        # Best effort close; a failure here is not actionable during shutdown.
        try:
            self.conn.close()
        except Exception:  # noqa: S110
            pass

    # ------------------------------------------------------------------ introspection
    def effect_status(self, key: str) -> str | None:
        assert self.journal is not None
        return self.journal.status(key)

    def responsibility_state(self, rid: str):
        assert self.responsibilities is not None
        r = self.responsibilities.get(rid)
        return r.state if r else None
