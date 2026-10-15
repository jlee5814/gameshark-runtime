"""Inference executor (v0.3 step 6): provider I/O off the coordinator thread.

The coordinator is the thread that owns the supervisor's SQLite connection (the daemon's
tick thread). It runs every router phase that reads or writes the journal; a fixed pool of
executor threads runs only the phases that block on a provider and touch no SQLite:

    coordinator  submit   begin: short circuit, select, call limits; claim a slot
    executor     load     tracker admit: lazy load or probe, replacement wait
    coordinator  reserve  fenced reservation, then generate (no I/O)
    executor     execute  stream, collect, verify chunks and result
    coordinator  settle   seal, then commit or abandon; fail over or finish

Each executor job returns a typed Completion through a bounded queue; pump, on the
coordinator, advances the call. Executors never hold the coordinator's connection (SQLite's
check_same_thread would refuse it anyway) and never write durable state, so a thread still
running after shutdown can produce nothing but a completion nobody reads.

Bounds. Slots are claimed without queueing (max_wait 0), so every job belongs to a call
that already holds a provider slot and no executor ever waits on a slot that another job
needs: no deadlock, and threads equal to the slot total never starve. In flight calls are
capped at completion_depth, and each call has at most one job outstanding, so a completion
put never blocks. A full provider or a full executor refuses with Overloaded; the owner
(an inbox event) stays pending and retries.

Async routing requires a ModelTracker, so health probes run inside load on an executor
rather than inside select on the coordinator.
"""

from __future__ import annotations

import itertools
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass

from gameshark.inference.journal import CallSpec
from gameshark.inference.router import Claim, InferenceRouter, RouteResult, RouteState
from gameshark.inference.types import Overloaded, ProviderError
from gameshark.lifecycle import CancelToken

LOAD, EXECUTE = "load", "execute"
ABANDONED = "Abandoned"


@dataclass(frozen=True)
class InferenceTask:
    """What an event handler asks for: one call and how to continue once it settles.

    on_done and on_error run on the coordinator, after the outcome is durable. The
    supervisor wraps them with event acknowledgment, so a handler never acks itself.
    """

    call_id: str
    conversation_id: str
    spec: CallSpec
    on_done: Callable[[RouteResult], None]
    on_error: Callable[[ProviderError], None]


@dataclass(frozen=True)
class Completion:
    task_id: int
    stage: str
    value: object = None
    error: BaseException | None = None


@dataclass(frozen=True)
class ExecutorStats:
    tasks: int
    jobs_queued: int
    completions_queued: int
    peak_completions: int
    threads_alive: int
    refused: int


class _Task:
    def __init__(self, task_id: int, task: InferenceTask, state: RouteState,
                 on_abort: Callable[[], None]) -> None:
        self.id = task_id
        self.task = task
        self.state = state
        self.on_abort = on_abort
        self.claim: Claim | None = None
        self.stage = ""


class InferenceExecutor:
    def __init__(self, router: InferenceRouter, cancel: CancelToken,
                 threads: int | None = None, completion_depth: int | None = None,
                 crash: Callable[[str], None] | None = None,
                 admit: Callable[[], None] | None = None,
                 wake: threading.Event | None = None) -> None:
        if router.tracker is None:
            raise ValueError("async inference needs a ModelTracker: health probes must run "
                             "on an executor, not inside select on the coordinator")
        slots = sum(s.slots for name, s in router.scheduler.stats().items()
                    if name in router.providers)
        self.router = router
        self.cancel = cancel
        self.threads = threads or max(1, slots)
        self.depth = completion_depth or self.threads
        if self.threads < 1 or self.depth < 1:
            raise ValueError("threads and completion_depth must be at least 1")
        self._crash = crash or (lambda _label: None)
        self._admit = admit
        # Set on every completion; the coordinator waits on it. The supervisor shares one
        # event with the gateway, so either source wakes the coordinator.
        self.wake = wake or threading.Event()
        self._jobs: queue.SimpleQueue[tuple | None] = queue.SimpleQueue()
        self._done: queue.Queue[Completion] = queue.Queue(maxsize=self.depth)
        self._peak_lock = threading.Lock()
        self._peak = 0
        self._refused = 0
        self._ids = itertools.count(1)
        self._closed = False
        self._tasks: dict[int, _Task] = {}  # coordinator only
        self._workers = [threading.Thread(target=self._work, name=f"inference-exec-{i}",
                                          daemon=True) for i in range(self.threads)]
        for t in self._workers:
            t.start()

    # ------------------------------------------------------------------ executor side
    def _work(self) -> None:
        while (job := self._jobs.get()) is not None:
            task_id, stage, fn, claim = job
            try:
                c = Completion(task_id, stage, value=fn(claim))
            except BaseException as exc:
                c = Completion(task_id, stage, error=exc)
            self._done.put_nowait(c)  # never full: one job per task, tasks <= depth
            with self._peak_lock:
                self._peak = max(self._peak, self._done.qsize())
            self.wake.set()

    def _enqueue(self, t: _Task, stage: str, fn: Callable[[Claim], object]) -> None:
        t.stage = stage
        self._jobs.put((t.id, stage, fn, t.claim))

    # ------------------------------------------------------------------ coordinator side
    def submit(self, task: InferenceTask,
               on_abort: Callable[[], None] = lambda: None) -> RouteResult | None:
        """Start a call. Return its committed result at once when it is already done
        (replay short circuit), else None: the outcome arrives through task callbacks.

        Raises Draining, Overloaded, or a selection refusal before anything is reserved.
        on_abort runs if the call is dropped without an outcome (abandon_all, or a non
        provider exception such as FencedError escaping pump).
        """
        if self._admit is not None:
            self._admit()
        if len(self._tasks) >= self.depth:
            self._refused += 1
            raise Overloaded(f"{len(self._tasks)} inference calls in flight",
                             retry_after=self.router.scheduler.retry_after())
        out = self.router.begin(task.call_id, task.conversation_id, task.spec, self.cancel)
        if isinstance(out, RouteResult):
            return out
        t = _Task(next(self._ids), task, out, on_abort)
        try:
            self._claim_next(t)
        except BaseException:
            out.close()
            raise
        self._tasks[t.id] = t
        return None

    def _claim_next(self, t: _Task) -> None:
        """Claim the next candidate and send it to load, or raise the final error."""
        state = t.state
        while not state.remaining:
            if not self.router.advance(state):
                raise state.error()
        t.claim = self.router.claim(state, 0.0)
        self._enqueue(t, LOAD, self.router.load)

    def pump(self) -> int:
        """Advance every call with a completion waiting. Return how many were handled.

        Provider errors become the call's outcome. Anything else (FencedError, an injected
        crash) drops that call without writing, then propagates to the caller.
        """
        self.wake.clear()
        handled = 0
        while True:
            try:
                c = self._done.get_nowait()
            except queue.Empty:
                return handled
            handled += 1
            t = self._tasks.get(c.task_id)
            if t is None or t.stage != c.stage:
                continue  # abandoned; a late answer is discarded
            try:
                self._advance(t, c)
            except ProviderError as exc:
                self._finish(t)
                t.task.on_error(exc)
            except BaseException:
                self._drop(t)
                raise

    def _advance(self, t: _Task, c: Completion) -> None:
        claim = t.claim
        assert claim is not None
        if c.stage == LOAD:
            if c.error is not None:
                claim.release()
                raise c.error
            if c.value is not None:  # the tracker refused: before reserve, costs nothing
                claim.release()
                return self._next(t, c.value)
            try:
                out = self.router.reserve(claim)
            except BaseException:
                claim.release()
                raise
            if out is not None:
                claim.release()
                return self._next(t, out)
            return self._enqueue(t, EXECUTE, self.router.execute)
        # The answer is in memory only: a kill here loses it and the owner reruns.
        self._crash("E3_after_provider")
        out = self.router.settle(claim, c.error if c.error is not None else c.value)
        return self._next(t, out)

    def _next(self, t: _Task, out: RouteResult | ProviderError) -> None:
        t.claim = None
        result = t.state.note(out)
        if result is None:
            return self._claim_next(t)
        if not result.committed.replayed:
            self._crash("E4_after_commit")
        self._finish(t)
        t.task.on_done(result)

    def _finish(self, t: _Task) -> None:
        self._tasks.pop(t.id, None)
        t.stage = ""
        if t.claim is not None:
            t.claim.release()
            t.claim = None
        t.state.close()

    def _drop(self, t: _Task) -> None:
        self._finish(t)
        t.on_abort()

    def abandon_all(self, write: bool = True) -> int:
        """Drop every call in flight. With write, settle each held reservation lost while
        the lease is still ours; without (ownership lost), touch nothing durable. Late
        completions are discarded. Return how many calls were dropped."""
        tasks = list(self._tasks.values())
        for t in tasks:
            res = t.claim.res if t.claim is not None else None
            try:
                if write and res is not None and t.stage == EXECUTE:
                    self.router.journal.abandon(res, ABANDONED)
            except Exception:
                write = False  # fenced: the successor's recovery settles the rest
            finally:
                if res is not None:
                    self.router.journal.forget(res)
                self._drop(t)
        return len(tasks)

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    def calls(self) -> list[tuple[str, str, str]]:
        """(call_id, conversation_id, stage) for every call in flight. Coordinator only."""
        return [(t.task.call_id, t.task.conversation_id, t.stage) for t in self._tasks.values()]

    def stats(self) -> ExecutorStats:
        alive = sum(t.is_alive() for t in self._workers)
        jobs = self._jobs.qsize()
        if self._closed:
            jobs = max(0, jobs - alive)  # each worker still running leaves its stop marker
        return ExecutorStats(len(self._tasks), jobs, self._done.qsize(), self._peak, alive,
                             self._refused)

    def close(self, timeout_s: float = 2.0) -> int:
        """Stop the workers. Return how many are still running a provider call after
        timeout_s; they are daemon threads without a connection and cannot write."""
        self._closed = True
        for _ in self._workers:
            self._jobs.put(None)
        per = timeout_s / max(1, len(self._workers))
        for t in self._workers:
            t.join(per)
        return sum(t.is_alive() for t in self._workers)
