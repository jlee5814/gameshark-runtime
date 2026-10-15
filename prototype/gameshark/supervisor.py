"""AgentSupervisor: wires the primitives into a recoverable runtime.

A supervisor instance models one process lifetime. A crash discards the instance; a
fresh instance on the same database models a restart. Nothing in memory survives, so
recovery is a pure function of durable SQLite state.
"""

from __future__ import annotations

import json
from pathlib import Path

from gameshark.adapters import Adapter, AdapterContext, AdapterRegistry
from gameshark.authz import GrantStore
from gameshark.clock import Clock
from gameshark.effects import Effect, EffectJournal
from gameshark.errors import CrashInjected, UncommittedOutput
from gameshark.events import EventRouter
from gameshark.identity import Identity, load_or_create_identity
from gameshark.inference.journal import InferenceJournal
from gameshark.lease import LeaseManager
from gameshark.lifecycle import Lifecycle
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

    # ------------------------------------------------------------------ crash hook
    def _maybe_crash(self, label: str) -> None:
        if label in self.crash_at:
            self.crash_at.discard(label)      # fire once so recovery can proceed
            raise CrashInjected(label)

    # ------------------------------------------------------------------ setup
    def add_adapter(self, adapter: Adapter) -> None:
        """Declare an adapter. Its declared caps are recorded durably at start()."""
        self._declared.append(adapter)

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
    def _dispatch(self, event: dict) -> None:
        adapter = self.registry.get(event["adapter"])
        adapter.on_event(event)

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
