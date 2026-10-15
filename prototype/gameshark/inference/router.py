"""Inference router (v0.3 step 3): choose which provider executes a call, then admit it.

    complete(call_id, conversation_id, spec, cancel)
      replay short circuit     a done call returns its committed message, no provider call
      select                   capability, policy, ordering, health; typed refusal if empty
      admit_call               per agent and per conversation in flight limits
      for each candidate:
        acquire slot           free slot, else bounded FIFO queue, else Overloaded
        reserve                journal write txn: cloud grant recheck, budget, attempt + ledger
        generate, collect      provider runs; chunks and result verified against the attempt
        commit                 one assistant message, degraded flag, ledger settled to actual

Policy refusals (EgressDenied, BudgetExceeded, BudgetUnenforceable) end the walk at once.
They never advance to the next candidate, so a denial can never turn into cloud fallback.
Only a retryable failure after dispatch, or a capability refusal from generate, moves on.

The router reads the journal and the grants table and writes only through the journal.
Providers see a ProviderRequest built from journal rows and nothing else (R1).

Token accounting. estimate_tokens counts whitespace words and is no bound on any real
tokenizer. A provider's TokenBound says how its prompt is sized for the reservation:

    TRUSTED_BYTES   UTF 8 bytes plus template overhead; the operator asserts a byte level
                    tokenizer, so the reservation bounds usage and budgets are hard
    ESTIMATE        whitespace estimate; budgets are soft. A paid provider under a budget
                    is refused (BudgetUnenforceable) unless the durable grant
                    inference.cloud.soft_budget exists. Local providers cost nothing and
                    run soft by default.

Actual usage always settles unmodified; an attempt above its reservation is reported as an
Overrun. A trusted provider that overruns is demoted to ESTIMATE for this process. An
attempt whose provider reported no usage settles 'unmetered' at its full reservation, never
at zero; it is a bound only when the provider's TokenBound is trusted.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from enum import Enum

from gameshark.authz import GrantStore
from gameshark.inference.budget import admit_budget
from gameshark.inference.journal import CallSpec, Committed, InferenceJournal, Reservation
from gameshark.inference.provider import Provider
from gameshark.inference.scheduler import DEFAULT_QUEUE_DEPTH, Scheduler
from gameshark.inference.tracker import ModelTracker, Use
from gameshark.inference.types import (
    AuthFailed,
    BudgetUnenforceable,
    Capabilities,
    CapabilityUnsupported,
    ContextTooLong,
    EgressDenied,
    HealthStatus,
    Locality,
    Message,
    ModelReplaced,
    ModelUnavailable,
    Privacy,
    ProviderCancelled,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
    prompt_bytes_bound,
)
from gameshark.lifecycle import CancelToken

GRANT_ADAPTER = "inference"
CLOUD_GRANT = "inference.cloud"
SOFT_BUDGET_GRANT = "inference.cloud.soft_budget"
AUTH_FAILED = "AuthFailed"
MAX_RESELECT = 2  # extra selections after a replacement; never consumes an attempt by itself


class BoundKind(Enum):
    TRUSTED_BYTES = "trusted_bytes"
    ESTIMATE = "estimate"


@dataclass(frozen=True)
class TokenBound:
    kind: BoundKind = BoundKind.ESTIMATE
    per_message: int = 8  # chat template tokens per message
    fixed: int = 16  # template tokens per request

    @property
    def trusted(self) -> bool:
        return self.kind is BoundKind.TRUSTED_BYTES

    def prompt(self, messages: Sequence[Message]) -> int:
        if self.trusted:
            return prompt_bytes_bound(messages, self.per_message, self.fixed)
        return sum(len(m.content.split()) for m in messages)


UNTRUSTED = TokenBound()


@dataclass(frozen=True)
class ProviderConfig:
    provider: Provider
    token_bound: TokenBound = UNTRUSTED
    slots: int | None = None  # None: capabilities().max_concurrency
    queue_depth: int = DEFAULT_QUEUE_DEPTH
    fingerprint: str = ""  # changes when credentials or endpoint config change

    @property
    def name(self) -> str:
        return self.provider.name


@dataclass(frozen=True)
class RouterPolicy:
    preference: tuple[str, ...] = ()  # provider names tried first, in order
    allowed: frozenset[str] | None = None  # model constraint: only these providers
    agent_in_flight: int = 4
    conversation_in_flight: int = 1
    max_queue_wait_s: float = 0.0  # 0: never queue, refuse with Overloaded at once
    health_ttl_s: float = 5.0
    health_timeout_s: float = 1.0


@dataclass(frozen=True)
class Candidate:
    config: ProviderConfig
    caps: Capabilities
    bound: TokenBound
    generation: int = 0  # tracker configuration generation; 0 without a tracker

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def cloud(self) -> bool:
        return self.caps.locality is Locality.CLOUD

    @property
    def paid(self) -> bool:
        return self.caps.price_in_micros > 0 or self.caps.price_out_micros > 0


@dataclass(frozen=True)
class Overrun:
    call_id: str
    attempt: int
    provider: str
    reserved_tokens: int
    actual_tokens: int
    reserved_micros: int
    actual_micros: int
    hard: bool  # True: a trusted bound was violated, and the provider was demoted


@dataclass(frozen=True)
class RouteResult:
    committed: Committed
    provider: str | None
    degraded: bool
    soft_budget: bool  # admitted under an untrusted prompt bound
    overrun: Overrun | None = None
    metered: bool = True  # False: no provider usage; charged its full reservation


class ProviderHealth:
    """Process local provider state: circuits, auth blocks, bound demotions, health cache."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._now = monotonic
        self._lock = threading.Lock()
        self._circuit_until: dict[str, float] = {}
        self._auth_blocked: dict[str, str] = {}  # provider -> fingerprint that failed
        self._demoted: set[str] = set()
        self._health: dict[str, tuple[float, HealthStatus]] = {}

    def open_circuit(self, name: str, seconds: float) -> None:
        with self._lock:
            until = self._now() + max(seconds, 0.0)
            self._circuit_until[name] = max(until, self._circuit_until.get(name, 0.0))

    def block_auth(self, name: str, fingerprint: str) -> None:
        with self._lock:
            self._auth_blocked[name] = fingerprint

    def demote(self, name: str) -> None:
        with self._lock:
            self._demoted.add(name)

    def demoted(self, name: str) -> bool:
        with self._lock:
            return name in self._demoted

    def reset(self, name: str) -> None:
        """Forget circuit, demotion, and cached health: a replacement is a new endpoint.
        Auth blocks stay; they already lapse when the config fingerprint changes."""
        with self._lock:
            self._circuit_until.pop(name, None)
            self._demoted.discard(name)
            self._health.pop(name, None)

    def _blocked_locked(self, cfg: ProviderConfig, now: float) -> bool:
        return (self._circuit_until.get(cfg.name, 0.0) > now
                or self._auth_blocked.get(cfg.name) == cfg.fingerprint)

    def blocked(self, cfg: ProviderConfig) -> bool:
        """An open rate limit circuit or an auth block for this config. No probe."""
        now = self._now()
        with self._lock:
            return self._blocked_locked(cfg, now)

    def usable(self, cfg: ProviderConfig, ttl_s: float, timeout_s: float) -> bool:
        now = self._now()
        with self._lock:
            if self._blocked_locked(cfg, now):
                return False
            cached = self._health.get(cfg.name)
        if cached is None or now - cached[0] >= ttl_s:
            status = cfg.provider.health(timeout_s).status
            with self._lock:
                self._health[cfg.name] = (now, status)
        else:
            status = cached[1]
        return status is not HealthStatus.DOWN


class InferenceRouter:
    def __init__(self, journal: InferenceJournal, providers: Sequence[ProviderConfig],
                 policy: RouterPolicy | None = None, scheduler: Scheduler | None = None,
                 health: ProviderHealth | None = None, grants: GrantStore | None = None,
                 admit: Callable[[], None] | None = None,
                 tracker: ModelTracker | None = None) -> None:
        names = [c.name for c in providers]
        if len(set(names)) != len(names):
            raise ValueError(f"provider names must be unique: {names}")
        if tracker is not None and (scheduler not in (None, tracker.scheduler)
                                    or health not in (None, tracker.health)):
            raise ValueError("the tracker must share the router's scheduler and health")
        self.journal = journal
        self.providers = {c.name: c for c in providers}
        self.policy = policy or RouterPolicy()
        self.tracker = tracker
        self.scheduler = tracker.scheduler if tracker else scheduler or Scheduler()
        self.health = tracker.health if tracker else health or ProviderHealth()
        # Same connection as the journal, so the in transaction grant recheck reads the
        # state the reservation commits against.
        self.grants = grants or GrantStore(journal.conn, journal.agent_id, journal.clock)
        self._admit = admit
        for c in providers:
            if tracker is not None:
                tracker.register(c)
                c = tracker.config(c.name)  # a replacement may have superseded c
            self.scheduler.configure(c.name, c.slots or c.provider.capabilities().max_concurrency,
                                     c.queue_depth)

    def _configs(self) -> list[tuple[ProviderConfig, int]]:
        """Active configurations: the tracker's when present, so a replacement is seen by
        every router sharing it."""
        if self.tracker is None:
            return [(c, 0) for c in self.providers.values()]
        names = self.providers.keys()
        return [(c, g) for c, g in self.tracker.configs() if c.name in names]

    # ------------------------------------------------------------------ selection
    def _bound(self, cfg: ProviderConfig) -> TokenBound:
        return UNTRUSTED if self.health.demoted(cfg.name) else cfg.token_bound

    def select(self, conversation_id: str, spec: CallSpec) -> list[Candidate]:
        """Ordered eligible candidates for the conversation's next call, or a typed refusal."""
        conv = self.journal.conversation(conversation_id)
        messages = self.journal.context(conversation_id, conv["next_seq"])
        privacy = Privacy(conv["privacy"])

        # 1. Capability: features, model constraint, context window under the prompt bound.
        fits: list[Candidate] = []
        too_long = False
        for cfg, generation in self._configs():
            if self.policy.allowed is not None and cfg.name not in self.policy.allowed:
                continue
            caps, bound = cfg.provider.capabilities(), self._bound(cfg)
            if not caps.supports(spec.needs):
                continue
            if bound.prompt(messages) + spec.max_tokens > caps.context_window:
                too_long = True
                continue
            fits.append(Candidate(cfg, caps, bound, generation))
        if not fits:
            if too_long:
                raise ContextTooLong(f"no allowed provider fits {conversation_id} plus "
                                     f"{spec.max_tokens} output tokens")
            raise CapabilityUnsupported(f"no allowed provider supports {sorted(spec.needs)}")

        # 2. Policy: privacy, the durable cloud grant, enforceable budgets.
        cloud_ok = privacy is Privacy.CLOUD_OK and self.grants.effective(GRANT_ADAPTER,
                                                                          CLOUD_GRANT)
        budgeted = bool(self.journal.budgets())
        soft_ok = self.grants.effective(GRANT_ADAPTER, SOFT_BUDGET_GRANT)
        allowed: list[Candidate] = []
        egress_denied = unenforceable = False
        for c in fits:
            if c.cloud and not cloud_ok:
                egress_denied = True
            elif c.paid and not c.bound.trusted and budgeted and not soft_ok:
                unenforceable = True
            else:
                allowed.append(c)
        if not allowed:
            if egress_denied:
                raise EgressDenied(f"{conversation_id} is {privacy.value}; cloud grant "
                                   f"effective: {cloud_ok}")
            assert unenforceable
            raise BudgetUnenforceable(
                "a budget applies, no paid candidate has a trusted token bound, and "
                f"{SOFT_BUDGET_GRANT} is not granted")

        # 3. Ordering: configured preference, then local first, then cheapest cloud.
        pref = {name: i for i, name in enumerate(self.policy.preference)}
        allowed.sort(key=lambda c: (pref.get(c.name, len(pref)), c.cloud,
                                    c.caps.price_in_micros + c.caps.price_out_micros, c.name))

        # 4. Health: open circuits, auth blocks, providers reporting down. With a tracker,
        # failed models cooling down; the rest load or probe lazily at admission.
        if self.tracker is not None:
            healthy = [c for c in allowed
                       if not self.health.blocked(c.config) and self.tracker.eligible(c.name)]
            if not healthy:
                names = [c.name for c in allowed]
                raise ModelUnavailable(f"every eligible model is unavailable: {names}",
                                       retry_after=self.tracker.retry_after(names))
            return healthy
        healthy = [c for c in allowed if self.health.usable(
            c.config, self.policy.health_ttl_s, self.policy.health_timeout_s)]
        if not healthy:
            raise ProviderUnavailable(
                f"every eligible provider is unavailable: {[c.name for c in allowed]}")
        return healthy

    # ------------------------------------------------------------------ routing
    def _admission(self, cand: Candidate) -> Callable:
        def admit(conn, tokens: int, micros: int, now: float) -> None:
            # Recheck at dispatch time inside the reserve txn: a revocation committed after
            # selection still stops the request (R7).
            if cand.cloud and not self.grants.effective(GRANT_ADAPTER, CLOUD_GRANT):
                raise EgressDenied(f"{CLOUD_GRANT} revoked before dispatch to {cand.name}")
            admit_budget(self.journal, conn, tokens, micros, now)

        return admit

    def _durable_auth_block(self, call_id: str) -> None:
        """Refuse to replay a call whose provider failed auth until its config changed."""
        row = self.journal.call_row(call_id)
        if row is None or not (row["error_kind"] or "").startswith(AUTH_FAILED + ":"):
            return
        _, name, fingerprint = row["error_kind"].split(":", 2)
        cfg = next((c for c, _ in self._configs() if c.name == name), None)
        if cfg is not None and cfg.fingerprint == fingerprint:
            raise AuthFailed(f"call {call_id} failed auth on {name}; awaiting a config change")

    def _overrun(self, res: Reservation, cand: Candidate) -> Overrun | None:
        row = self.journal.conn.execute(
            "SELECT tokens_in, tokens_out, cost_micros FROM budget_ledger "
            "WHERE call_id=? AND attempt=?", (res.call_id, res.attempt)).fetchone()
        actual = (row["tokens_in"] or 0) + (row["tokens_out"] or 0)
        cost = row["cost_micros"] or 0
        if actual <= res.reserved_tokens and cost <= res.reserved_micros:
            return None
        if cand.bound.trusted:
            self.health.demote(cand.name)
        return Overrun(res.call_id, res.attempt, cand.name, res.reserved_tokens, actual,
                       res.reserved_micros, cost, hard=cand.bound.trusted)

    def complete(self, call_id: str, conversation_id: str, spec: CallSpec,
                 cancel: CancelToken) -> RouteResult:
        """Return the committed answer for call_id, routing and admitting new attempts.

        Raises a typed ProviderError when no attempt can run or the last one failed; the
        durable attempt cap (3) holds across calls and restarts.
        """
        if self._admit is not None:
            self._admit()
        done = self.journal.lookup(call_id)
        if done is not None:
            row = self.journal.call_row(call_id)
            return RouteResult(done, row["provider"], bool(row["degraded"]), False)
        self._durable_auth_block(call_id)
        candidates = self.select(conversation_id, spec)
        first = candidates[0].name
        with self.scheduler.admit_call(self.journal.agent_id, conversation_id,
                                       self.policy.agent_in_flight,
                                       self.policy.conversation_in_flight):
            out, replaced = self._walk(call_id, conversation_id, spec, cancel, candidates, first)
            for _ in range(MAX_RESELECT):
                if not replaced or isinstance(out, RouteResult):
                    break
                # A configuration changed under the walk: select again, so capability,
                # policy, and the egress grant are judged against the active configuration.
                # Bounded: each round waits for the pending activation it raced.
                out, replaced = self._walk(call_id, conversation_id, spec, cancel,
                                           self.select(conversation_id, spec), first)
        if isinstance(out, RouteResult):
            return out
        raise out if out is not None else ProviderUnavailable("no candidate ran")

    def _walk(self, call_id: str, conversation_id: str, spec: CallSpec, cancel: CancelToken,
              candidates: list[Candidate], first: str
              ) -> tuple[RouteResult | ProviderError | None, bool]:
        by_name = {c.name: c for c in candidates}
        remaining = [c.name for c in candidates]
        last: ProviderError | None = None
        replaced = False
        while remaining:
            slot = self.scheduler.acquire(remaining, cancel, self.policy.max_queue_wait_s)
            with slot:
                cand = by_name[slot.provider]
                remaining.remove(cand.name)
                out = self._attempt(call_id, conversation_id, spec, cancel, cand, first)
            if isinstance(out, RouteResult):
                return out, replaced
            replaced = replaced or isinstance(out, ModelReplaced)
            last = out
        return last, replaced

    def _attempt(self, call_id: str, conversation_id: str, spec: CallSpec,
                 cancel: CancelToken, cand: Candidate, first: str) -> RouteResult | ProviderError:
        """Run one attempt on one candidate. Return the result, or a retryable error to fail
        over on; raise anything terminal."""
        use: Use | None = None
        if self.tracker is not None:
            try:  # lazy load or probe; before reserve, so a refusal costs nothing
                use = self.tracker.admit(cand.name, cand.generation, cancel)
            except ModelUnavailable as exc:
                return exc
        with use or nullcontext():
            return self._dispatch(call_id, conversation_id, spec, cancel, cand, first, use)

    def _dispatch(self, call_id: str, conversation_id: str, spec: CallSpec,
                  cancel: CancelToken, cand: Candidate, first: str,
                  use: Use | None) -> RouteResult | ProviderError:
        journal = self.journal
        token = use.token if use is not None else cancel
        res = journal.reserve(call_id, conversation_id, cand.name, cand.caps, spec,
                              prompt_tokens=cand.bound.prompt, admit=self._admission(cand))
        if isinstance(res, Committed):  # another owner finished it meanwhile
            row = journal.call_row(call_id)
            return RouteResult(res, row["provider"], bool(row["degraded"]), False)
        try:
            call = cand.config.provider.generate(res.request, token)
        except ProviderError as exc:
            journal.unreserve(res)  # generate does no I/O: nothing dispatched, nothing spent
            if isinstance(exc, (CapabilityUnsupported, ContextTooLong)):
                return exc  # this provider disagrees with its declared capabilities
            raise
        try:
            result = journal.collect(res, use.watch(call) if use is not None else call)
        except ProviderError as exc:
            if use is not None:
                use.observe(exc)
                if use.evicted and not cancel.cancelled:
                    # The replacement cancelled this call, not the operator: retryable on the
                    # successor configuration.
                    journal.abandon(res, ModelReplaced.__name__)
                    return ModelReplaced(f"{cand.name} replaced during call {call_id}")
            if isinstance(exc, AuthFailed):
                self.health.block_auth(cand.name, cand.config.fingerprint)
                journal.abandon(res, f"{AUTH_FAILED}:{cand.name}:{cand.config.fingerprint}")
                raise
            journal.abandon(res, type(exc).__name__)
            if isinstance(exc, RateLimited):
                self.health.open_circuit(cand.name, exc.retry_after)
            if isinstance(exc, ProviderCancelled) or not exc.retryable:
                raise
            return exc
        except BaseException:
            journal.abandon(res)
            raise
        finally:
            call.close()
        if use is not None:
            if not use.seal():
                # Evicted before it could commit: a provider that ignored the cancel answered
                # for the retired incarnation. Settle lost and rerun on the successor.
                journal.abandon(res, ModelReplaced.__name__)
                if cancel.cancelled:
                    raise ProviderCancelled(cancel.reason)
                return ModelReplaced(f"{cand.name} replaced during call {call_id}")
            use.observe(None)
            # Audit the tracker's incarnation: providers of external servers cannot see one.
            result = dataclasses.replace(result, instance_ref=use.instance_ref)
        committed = journal.commit(res, result, degraded=cand.name != first)
        metered = result.usage is not None
        return RouteResult(committed, cand.name, cand.name != first,
                           soft_budget=not cand.bound.trusted,
                           overrun=self._overrun(res, cand) if metered else None,
                           metered=metered)
