"""Model lifecycle tracker (v0.3 step 5): availability, crash, recovery, replacement.

Memory only. A restart builds every configured model at discovered; conversations,
responsibilities, and the ledger stay in SQLite, which this module never touches.
Externally managed servers only: GameShark never starts, kills, or unloads a process,
so loading is a readiness check and unloading only stops routing.

    discovered ──demand──▶ loading ──plan ok──▶ ready ──admit──▶ executing ──last exit──▶ idle
                            │  ▲ next plan                ▲                      │
                            │  └──────────┘               └──── last exit ───────┘ (no token yet)
                            └── third plan fails ──▶ failed ──cooldown──▶ discovered
    ready | idle | executing ── probe down ──▶ crashed ──demand──▶ loading (new incarnation)
    ready | idle ── model no longer listed ──▶ unloaded ──demand──▶ loading
    any live state ── replace ──▶ unloading ──drained──▶ unloaded (retired; successor active)

Three readiness levels, never conflated:

    ENDPOINT   GET /v1/models answered: the server process is up
    LISTED     the model id is in that list: the server routes the name. LM Studio with
               JIT and mlx_lm.server list models that are not loaded
    RESIDENT   a call on this incarnation streamed a chunk: the only evidence of weights
               in GPU memory that the OpenAI API offers

A failed request is not a crash. Transport failures only raise a suspicion; the next
demand probes before admitting, and only an endpoint that no longer answers is crashed.

The tracker holds no slots and enforces no limit. Scheduler owns slots and queues;
in_flight here exists only so a replacement knows when its drain finished.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Self

from gameshark.inference.types import (
    Chunk,
    HealthStatus,
    ModelCrashed,
    ModelReplaced,
    ModelUnavailable,
    ProviderCancelled,
    ProviderError,
    ProviderResult,
    ProviderUnavailable,
    StreamInterrupted,
)
from gameshark.lifecycle import CancelToken

if TYPE_CHECKING:
    from gameshark.inference.provider import ProviderCall
    from gameshark.inference.router import ProviderConfig, ProviderHealth
    from gameshark.inference.scheduler import Scheduler

HISTORY = 256
DETAIL_CHARS = 200
TRANSPORT_ERRORS = (ProviderUnavailable, StreamInterrupted, ModelCrashed)


class ModelState(Enum):
    DISCOVERED = "discovered"
    LOADING = "loading"
    READY = "ready"
    EXECUTING = "executing"
    IDLE = "idle"
    UNLOADING = "unloading"
    UNLOADED = "unloaded"
    CRASHED = "crashed"
    FAILED = "failed"


S = ModelState
_ALLOWED: dict[ModelState, frozenset[ModelState]] = {
    S.DISCOVERED: frozenset({S.LOADING, S.UNLOADING}),
    S.LOADING: frozenset({S.LOADING, S.READY, S.FAILED, S.UNLOADING}),
    S.READY: frozenset({S.EXECUTING, S.CRASHED, S.UNLOADED, S.UNLOADING}),
    S.EXECUTING: frozenset({S.IDLE, S.READY, S.CRASHED, S.UNLOADING}),
    S.IDLE: frozenset({S.EXECUTING, S.CRASHED, S.UNLOADED, S.UNLOADING}),
    S.UNLOADING: frozenset({S.UNLOADED}),
    S.UNLOADED: frozenset({S.LOADING, S.FAILED, S.UNLOADING}),
    S.CRASHED: frozenset({S.LOADING, S.FAILED, S.UNLOADING}),
    S.FAILED: frozenset({S.DISCOVERED, S.UNLOADING}),
}
_SERVING = frozenset({S.READY, S.EXECUTING, S.IDLE})


class IllegalTransition(RuntimeError):
    """A lifecycle move outside the transition table. A tracker bug, never a provider fault."""


class Readiness(Enum):
    ENDPOINT = 1
    LISTED = 2
    RESIDENT = 3


@dataclass(frozen=True)
class LoadPlan:
    """One startup attempt. Plans in a cycle must differ, so every retry changes something."""

    name: str
    probe_timeout_s: float
    delay_s: float = 0.0  # wait before probing, for a server still loading weights
    accept_unlisted: bool = False  # endpoint up is enough; the first request loads (JIT)

    @property
    def key(self) -> tuple[float, float, bool]:
        return (self.probe_timeout_s, self.delay_s, self.accept_unlisted)

    def accepts(self, level: Readiness | None) -> bool:
        if level is None:
            return False
        return level is not Readiness.ENDPOINT or self.accept_unlisted


DEFAULT_PLANS = (
    LoadPlan("probe", probe_timeout_s=1.0),
    LoadPlan("patient", probe_timeout_s=10.0, delay_s=2.0),
    LoadPlan("endpoint_only", probe_timeout_s=10.0, delay_s=2.0, accept_unlisted=True),
)
MAX_PLANS = 3


@dataclass(frozen=True)
class TrackerPolicy:
    plans: tuple[LoadPlan, ...] = DEFAULT_PLANS
    cooldown_s: float = 300.0  # failed models stay out of routing this long
    verify_ttl_s: float = 30.0  # an unused ready or idle model is probed again after this
    load_wait_s: float = 30.0  # how long a caller waits for a load or probe
    replace_wait_s: float = 5.0  # how long a caller waits for a replacement to activate
    drain_grace_s: float = 2.0  # after cancelling undrained calls, wait this long for exit
    max_recoveries: int = 3  # consecutive crash cycles without a completed call

    def __post_init__(self) -> None:
        if not 1 <= len(self.plans) <= MAX_PLANS:
            raise ValueError(f"a load cycle has 1 to {MAX_PLANS} plans")
        seen: set[tuple[float, float, bool]] = set()
        for p in self.plans:
            if p.key in seen:
                raise ValueError(f"plan {p.name} changes nothing from an earlier plan")
            seen.add(p.key)
        if self.max_recoveries < 1:
            raise ValueError("max_recoveries must be at least 1")


@dataclass(frozen=True)
class Transition:
    name: str
    generation: int
    source: ModelState
    target: ModelState
    reason: str
    at: float


@dataclass(frozen=True)
class ReplaceReport:
    generation: int  # the new active configuration
    drained: int  # calls that finished on the old configuration
    cancelled: int  # calls cancelled at the drain deadline
    abandoned: int  # calls still running after the grace period


@dataclass
class ModelRecord:
    config: ProviderConfig
    generation: int
    state: ModelState = ModelState.DISCOVERED
    readiness: Readiness | None = None
    incarnation: int = 0
    plan: LoadPlan | None = None
    in_flight: int = 0
    uses: set[Use] = field(default_factory=set)
    suspect: str | None = None
    recoveries: int = 0
    cooldown_until: float = 0.0
    verified_at: float = 0.0
    last_used: float = 0.0
    request_failures: int = 0
    transport_failures: int = 0
    crashes: int = 0
    probes: int = 0
    loading: bool = False  # a load or probe job is running
    replacing: bool = False  # a replacement retired this record

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def instance_ref(self) -> str:
        return f"{self.name}:{self.generation}.{self.incarnation}"


@dataclass(frozen=True)
class ModelView:
    """A consistent snapshot of one record for observation and tests."""

    name: str
    generation: int
    state: ModelState
    readiness: Readiness | None
    incarnation: int
    instance_ref: str
    in_flight: int
    suspect: str | None
    recoveries: int
    cooldown_until: float
    request_failures: int
    transport_failures: int
    crashes: int
    probes: int


def _wait_on_token(token: CancelToken, seconds: float) -> bool:
    return token.wait(seconds)


class Use:
    """One admitted call on one record. Release is idempotent and runs on every exit path.

    token is a child of the caller's token: the caller's cancel reaches the provider, and a
    replacement can cancel this call alone at its drain deadline (evicted is then True).
    Cancellation is a request: a remote server may keep executing and even answer.

    The commit boundary is the tracker lock. A finished call must seal its use before its
    result commits; eviction takes the same lock and skips a sealed use. Exactly one wins:
    a sealed call counts as drained and commits on this incarnation, and an evicted call's
    late result, successful or not, is discarded and its attempt settled lost.
    """

    def __init__(self, tracker: ModelTracker, record: ModelRecord, cancel: CancelToken) -> None:
        self.tracker = tracker
        self.record = record
        self.incarnation = record.incarnation
        self.instance_ref = record.instance_ref
        self.token = CancelToken()
        self.evicted = False
        self.sealed = False
        self._released = False
        self._unlink = cancel.on_cancel(lambda: self.token.cancel(cancel.reason))

    def seal(self) -> bool:
        """Claim the right to commit. False when a replacement evicted this call first."""
        with self.tracker._cond:
            if self.evicted:
                return False
            self.sealed = True
            return True

    def evict(self, reason: str) -> bool:
        """Cancel this call for a replacement. False, and no cancel, when already sealed."""
        with self.tracker._cond:
            if self.sealed:
                return False
            self.evicted = True
        self.token.cancel(reason)  # outside the lock: token callbacks may take it
        return True

    def watch(self, call: ProviderCall) -> ProviderCall:
        """Wrap a call so its first chunk confirms residency for this incarnation."""
        return _Watched(call, self)

    def observe(self, exc: BaseException | None) -> None:
        self.tracker._observe(self, exc)

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._unlink()
            self.tracker._release(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class _Watched:
    def __init__(self, call: ProviderCall, use: Use) -> None:
        self.request = call.request
        self._call = call
        self._use = use

    def __iter__(self) -> Iterator[Chunk]:
        first = True
        for chunk in self._call:
            if first:
                first = False
                self._use.tracker._resident(self._use)
            yield chunk

    def result(self) -> ProviderResult:
        return self._call.result()

    def close(self) -> None:
        self._call.close()


class ModelTracker:
    """One in memory record per configured model; the active configuration per name.

    monotonic drives policy time (cooldown, verify TTL, history). Blocking waits (load,
    replacement, drain) are bounded in real time, as Scheduler's queue waits are. wait
    sleeps between plans and returns early when the tracker closes.
    """

    def __init__(self, scheduler: Scheduler, health: ProviderHealth,
                 policy: TrackerPolicy | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 wait: Callable[[CancelToken, float], bool] = _wait_on_token) -> None:
        self.scheduler = scheduler
        self.health = health
        self.policy = policy or TrackerPolicy()
        self._now = monotonic
        self._wait = wait
        self._cond = threading.Condition(threading.Lock())
        self._active: dict[str, ModelRecord] = {}
        self._history: dict[str, deque[Transition]] = {}
        self._known: dict[str, list[ProviderConfig]] = {}  # every config held per name
        self._jobs: list[threading.Thread] = []
        self._stop = CancelToken()

    # ------------------------------------------------------------------ configuration
    def register(self, cfg: ProviderConfig) -> None:
        """Add a configuration at discovered. A config the tracker already held under
        this name is a no op, so a router built from a stale list never undoes a
        replacement; any other config under a known name is refused: use replace."""
        with self._cond:
            known = self._known.get(cfg.name)
            if known is None:
                self._active[cfg.name] = ModelRecord(cfg, generation=1)
                self._history[cfg.name] = deque(maxlen=HISTORY)
                self._known[cfg.name] = [cfg]
            elif cfg not in known:
                raise ValueError(f"{cfg.name} is configured; replace it instead")

    def configs(self) -> list[tuple[ProviderConfig, int]]:
        with self._cond:
            return [(r.config, r.generation) for r in self._active.values()]

    def config(self, name: str) -> ProviderConfig:
        with self._cond:
            return self._active[name].config

    # ------------------------------------------------------------------ observation
    def view(self, name: str) -> ModelView:
        with self._cond:
            self._expire_locked(self._active[name])
            return self._view_locked(self._active[name])

    @staticmethod
    def _view_locked(r: ModelRecord) -> ModelView:
        return ModelView(r.name, r.generation, r.state, r.readiness, r.incarnation,
                         r.instance_ref, r.in_flight, r.suspect, r.recoveries,
                         r.cooldown_until, r.request_failures, r.transport_failures,
                         r.crashes, r.probes)

    def state(self, name: str) -> ModelState:
        return self.view(name).state

    def history(self, name: str) -> list[Transition]:
        with self._cond:
            return list(self._history[name])

    def path(self, name: str, generation: int | None = None) -> list[ModelState]:
        """States a record passed through, starting at discovered."""
        hist = [t for t in self.history(name) if generation is None or t.generation == generation]
        return [ModelState.DISCOVERED] + [t.target for t in hist]

    def eligible(self, name: str) -> bool:
        """False only while a failed model cools down. Discovered, crashed, and unloaded
        models stay eligible: they load lazily on demand."""
        with self._cond:
            rec = self._active[name]
            self._expire_locked(rec)
            return rec.state is not ModelState.FAILED

    def retry_after(self, names: Sequence[str]) -> float:
        now = self._now()
        with self._cond:
            waits = [r.cooldown_until - now for n in names
                     if (r := self._active.get(n)) and r.state is ModelState.FAILED]
        return max(min(waits), 0.0) if waits else 0.0

    # ------------------------------------------------------------------ transitions
    def _move(self, rec: ModelRecord, to: ModelState, reason: str) -> None:
        if to not in _ALLOWED[rec.state]:
            raise IllegalTransition(f"{rec.name} gen {rec.generation}: {rec.state.value} -> "
                                    f"{to.value} ({reason})")
        self._history[rec.name].append(
            Transition(rec.name, rec.generation, rec.state, to, reason, self._now()))
        rec.state = to
        self._cond.notify_all()

    def _expire_locked(self, rec: ModelRecord) -> None:
        if rec.state is ModelState.FAILED and self._now() >= rec.cooldown_until:
            rec.recoveries = 0
            self._move(rec, ModelState.DISCOVERED, "cooldown elapsed")

    def _fail_locked(self, rec: ModelRecord, reason: str) -> None:
        rec.cooldown_until = self._now() + self.policy.cooldown_s
        rec.readiness = None
        self._move(rec, ModelState.FAILED, reason)

    def _job_kind(self, rec: ModelRecord) -> str | None:
        if rec.state in (ModelState.DISCOVERED, ModelState.CRASHED, ModelState.UNLOADED):
            return "load"
        if rec.state in _SERVING and rec.suspect is not None:
            return "verify"
        if (rec.state in (ModelState.READY, ModelState.IDLE)
                and self._now() - rec.verified_at >= self.policy.verify_ttl_s):
            return "verify"
        return None

    # ------------------------------------------------------------------ admission
    def admit(self, name: str, generation: int, cancel: CancelToken) -> Use:
        """Admit one call on the active record of name, loading or probing it first if
        needed. Raises before dispatch: ModelReplaced when generation is no longer active,
        ModelUnavailable when failed, cooling, or not ready within the wait bound, and
        ProviderCancelled when cancel fires while waiting."""
        start = time.monotonic()
        unsubscribe = cancel.on_cancel(self._wake)
        try:
            with self._cond:
                while True:
                    if cancel.cancelled:
                        raise ProviderCancelled(cancel.reason)
                    rec = self._active[name]
                    if rec.generation != generation:
                        raise ModelReplaced(f"{name} gen {generation} was replaced by gen "
                                            f"{rec.generation}")
                    if rec.state is ModelState.UNLOADING:
                        left = self.policy.replace_wait_s - (time.monotonic() - start)
                        if left <= 0:
                            raise ModelUnavailable(f"{name} is being replaced",
                                                   retry_after=self.policy.replace_wait_s)
                        self._cond.wait(left)
                        continue
                    self._expire_locked(rec)
                    if rec.state is ModelState.FAILED:
                        raise ModelUnavailable(
                            f"{name} failed to load; cooling down",
                            retry_after=max(rec.cooldown_until - self._now(), 0.0))
                    if not rec.loading:
                        kind = self._job_kind(rec)
                        if kind is not None:
                            self._start_locked(rec, kind)
                    if rec.loading:
                        left = self.policy.load_wait_s - (time.monotonic() - start)
                        if left <= 0:
                            raise ModelUnavailable(f"{name} not ready within "
                                                   f"{self.policy.load_wait_s}s")
                        self._cond.wait(left)
                        continue
                    if rec.state in _SERVING:
                        use = Use(self, rec, cancel)
                        rec.uses.add(use)
                        rec.in_flight += 1
                        rec.last_used = self._now()
                        if rec.state is not ModelState.EXECUTING:
                            self._move(rec, ModelState.EXECUTING, "call admitted")
                        return use
                    raise ModelUnavailable(f"{name} is {rec.state.value}")  # tracker closed
        finally:
            unsubscribe()

    def _wake(self) -> None:
        with self._cond:
            self._cond.notify_all()

    # ------------------------------------------------------------------ load and probe jobs
    def _start_locked(self, rec: ModelRecord, kind: str) -> None:
        rec.loading = True
        t = threading.Thread(target=self._job, args=(rec, kind), daemon=True,
                             name=f"model-{kind}-{rec.name}")
        self._jobs = [j for j in self._jobs if j.is_alive()] + [t]
        t.start()

    def _probe(self, rec: ModelRecord, plan: LoadPlan) -> tuple[Readiness | None, str]:
        try:
            h = rec.config.provider.health(plan.probe_timeout_s)
        except Exception as exc:  # a probe never raises into the tracker
            return None, f"{type(exc).__name__}: {exc}"[:DETAIL_CHARS]
        finally:
            with self._cond:
                rec.probes += 1
        level = {HealthStatus.OK: Readiness.LISTED,
                 HealthStatus.DEGRADED: Readiness.ENDPOINT}.get(h.status)
        return level, h.detail[:DETAIL_CHARS]

    def _job(self, rec: ModelRecord, kind: str) -> None:
        try:
            if kind == "verify" and not self._verify(rec):
                return
            self._load(rec)
        finally:
            with self._cond:
                rec.loading = False
                self._cond.notify_all()

    def _verify(self, rec: ModelRecord) -> bool:
        """Probe a serving record. Return True when it must load again."""
        plan = rec.plan or self.policy.plans[0]
        level, detail = self._probe(rec, plan)
        with self._cond:
            if rec.state not in _SERVING:  # replaced meanwhile
                return False
            cause = rec.suspect or "verify ttl"
            rec.suspect = None
            if plan.accepts(level):
                rec.verified_at = self._now()
                return False
            if level is None:
                rec.crashes += 1
                rec.recoveries += 1
                rec.readiness = None
                self._move(rec, ModelState.CRASHED, f"probe down after {cause}: {detail}")
                if rec.recoveries >= self.policy.max_recoveries:
                    self._fail_locked(rec, f"{rec.recoveries} crash cycles without a "
                                           "completed call")
                    return False
                return True
            rec.readiness = None
            self._move(rec, ModelState.UNLOADED, f"model no longer listed: {detail}")
            return True

    def _load(self, rec: ModelRecord) -> None:
        plans = self.policy.plans
        with self._cond:
            if rec.state not in (ModelState.DISCOVERED, ModelState.CRASHED,
                                 ModelState.UNLOADED):
                return
            self._move(rec, ModelState.LOADING, f"demand; plan {plans[0].name}")
        for i, plan in enumerate(plans):
            if plan.delay_s > 0 and self._wait(self._stop, plan.delay_s):
                return
            level, detail = self._probe(rec, plan)
            with self._cond:
                if rec.state is not ModelState.LOADING:  # replaced meanwhile
                    return
                if plan.accepts(level):
                    rec.readiness, rec.plan = level, plan
                    rec.incarnation += 1
                    rec.verified_at = self._now()
                    self._move(rec, ModelState.READY, f"plan {plan.name} passed at "
                                                      f"{level.name.lower()}")
                    return
                seen = level.name.lower() if level else "down"
                if i + 1 < len(plans):
                    self._move(rec, ModelState.LOADING,
                               f"plan {plan.name} failed ({seen}: {detail}); "
                               f"plan {plans[i + 1].name}")
                else:
                    self._fail_locked(rec, f"plan {plan.name} failed ({seen}: {detail}); "
                                           f"{len(plans)} plans exhausted")

    # ------------------------------------------------------------------ call outcomes
    def _resident(self, use: Use) -> None:
        with self._cond:
            rec = use.record
            if use.incarnation == rec.incarnation and rec.state in _SERVING:
                rec.readiness = Readiness.RESIDENT

    def _observe(self, use: Use, exc: BaseException | None) -> None:
        """Classify a call outcome. A transport failure only raises a suspicion that the
        next demand verifies; request failures never touch the state."""
        with self._cond:
            rec = use.record
            if exc is None:
                rec.recoveries = 0
                rec.verified_at = self._now()
                return
            if use.evicted or isinstance(exc, ProviderCancelled):
                return
            if isinstance(exc, TRANSPORT_ERRORS) and use.incarnation == rec.incarnation:
                rec.transport_failures += 1
                if rec.state in _SERVING:
                    rec.suspect = f"{type(exc).__name__}: {exc}"[:DETAIL_CHARS]
            elif isinstance(exc, ProviderError):
                rec.request_failures += 1

    def _release(self, use: Use) -> None:
        with self._cond:
            rec = use.record
            rec.uses.discard(use)
            rec.in_flight -= 1
            rec.last_used = self._now()
            if rec.in_flight == 0 and rec.state is ModelState.EXECUTING:
                if rec.readiness is Readiness.RESIDENT:
                    self._move(rec, ModelState.IDLE, "last call ended")
                else:
                    self._move(rec, ModelState.READY, "last call ended before any token")
            self._cond.notify_all()

    # ------------------------------------------------------------------ replacement
    def replace(self, cfg: ProviderConfig, drain_timeout_s: float) -> ReplaceReport:
        """Replace the configuration under cfg.name. Blocks until the old one drained.

        New admissions to the old configuration stop at once; admitted calls finish within
        drain_timeout_s or are cancelled through their own tokens. The successor starts at
        discovered and loads on the next demand. Callers waiting meanwhile get ModelReplaced
        and select again.

        Every successor setting is validated before the old record changes, so a malformed
        cfg raises ValueError and leaves the active model serving. Once the old record is
        unloading the transition only moves forward: an exception during the drain still
        installs the successor before it propagates, so the name never stays unloading and
        the retired configuration never serves again.
        """
        name = cfg.name
        slots = cfg.slots or cfg.provider.capabilities().max_concurrency
        self.scheduler.check(slots, cfg.queue_depth)
        with self._cond:
            old = self._active.get(name)
            if old is None:
                raise ValueError(f"{name} is not configured")
            if old.replacing:
                raise RuntimeError(f"{name} is already being replaced")
            old.replacing = True
            self._move(old, ModelState.UNLOADING, "replacement requested")
            admitted = old.in_flight
        cancelled = abandoned = 0
        try:
            with self._cond:
                deadline = time.monotonic() + drain_timeout_s
                while old.in_flight and (left := deadline - time.monotonic()) > 0:
                    self._cond.wait(left)
                stuck = list(old.uses)
            for use in stuck:
                cancelled += use.evict(f"replaced: {name} gen {old.generation} drain deadline")
            with self._cond:
                deadline = time.monotonic() + self.policy.drain_grace_s
                while old.in_flight and (left := deadline - time.monotonic()) > 0:
                    self._cond.wait(left)
        finally:
            with self._cond:
                # Retire and install in one critical section: admission never sees the old
                # record unloaded and loadable while it is still active.
                abandoned = old.in_flight
                old.readiness = None
                self._move(old, ModelState.UNLOADED, f"drained; {abandoned} still running")
                new = ModelRecord(cfg, generation=old.generation + 1)
                self._active[name] = new
                self._known[name].append(cfg)
                self.scheduler.configure(name, slots, cfg.queue_depth)  # checked above
                self.health.reset(name)
                self._cond.notify_all()
        return ReplaceReport(new.generation, admitted - cancelled, cancelled, abandoned)

    # ------------------------------------------------------------------ shutdown
    def close(self, timeout_s: float = 5.0) -> None:
        """Stop waiting between plans and join the job threads."""
        self._stop.cancel("tracker closed")
        for t in list(self._jobs):
            t.join(timeout_s)
