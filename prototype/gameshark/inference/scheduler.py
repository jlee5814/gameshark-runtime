"""In memory admission for inference: provider slots, bounded FIFO queues, call limits.

Nothing here is durable. Slots, queues, and counters describe one process; a restart
starts empty, and the durable journal (attempts, ledger) carries everything that must
survive. One Scheduler may serve several routers on several threads.

    ticket = scheduler.admit_call(agent, conversation, agent_limit, conversation_limit)
    with ticket:                              # per agent and per conversation in flight
        slot = scheduler.acquire(names, cancel, max_wait_s)
        with slot: ...                        # one provider slot, released on any exit

A full provider hands its next free slot straight to the head of its queue, so order is
strict FIFO and no waiter spins or races a newcomer. Every refusal is a typed Overloaded
carrying retry_after; nothing waits without a bound.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Self

from gameshark.inference.types import Overloaded, ProviderCancelled
from gameshark.lifecycle import CancelToken

DEFAULT_QUEUE_DEPTH = 16
DEFAULT_LATENCY_S = 1.0
LATENCY_SAMPLES = 64


@dataclass
class _Waiter:
    provider: str
    event: threading.Event = field(default_factory=threading.Event)
    granted: bool = False


@dataclass
class _Pool:
    slots: int
    queue_depth: int
    in_flight: int = 0
    queue: deque[_Waiter] = field(default_factory=deque)
    peak_in_flight: int = 0
    peak_queue: int = 0
    latencies: deque[float] = field(default_factory=lambda: deque(maxlen=LATENCY_SAMPLES))


@dataclass(frozen=True)
class PoolStats:
    slots: int
    in_flight: int
    queued: int
    peak_in_flight: int
    peak_queue: int
    queue_depth: int


class Slot:
    """One held provider slot. Release is idempotent and runs on every exit path."""

    def __init__(self, scheduler: Scheduler, provider: str, started: float) -> None:
        self.scheduler = scheduler
        self.provider = provider
        self._started = started
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self.scheduler._release(self.provider, self._started)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class CallTicket:
    """Per agent and per conversation in flight accounting for one routed call."""

    def __init__(self, scheduler: Scheduler, agent_id: str, conversation_id: str) -> None:
        self.scheduler = scheduler
        self.agent_id = agent_id
        self.conversation_id = conversation_id
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self.scheduler._end_call(self.agent_id, self.conversation_id)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class Scheduler:
    def __init__(self, monotonic: Callable[[], float] = time.monotonic,
                 retry_after_cap_s: float = 60.0,
                 default_latency_s: float = DEFAULT_LATENCY_S) -> None:
        self._now = monotonic
        self._cap = retry_after_cap_s
        self._default_latency = default_latency_s
        self._lock = threading.Lock()
        self._pools: dict[str, _Pool] = {}
        self._agents: dict[str, int] = {}
        self._conversations: dict[str, int] = {}

    # ------------------------------------------------------------------ config
    @staticmethod
    def check(slots: int, queue_depth: int) -> None:
        """Raise ValueError for settings configure would refuse. Touches no state."""
        if slots < 1 or queue_depth < 0:
            raise ValueError("slots must be at least 1 and queue_depth not negative")

    def configure(self, provider: str, slots: int,
                  queue_depth: int = DEFAULT_QUEUE_DEPTH) -> None:
        """Declare a provider's slots and queue bound. Reconfiguring keeps live counts."""
        self.check(slots, queue_depth)
        with self._lock:
            pool = self._pools.get(provider)
            if pool is None:
                self._pools[provider] = _Pool(slots, queue_depth)
            else:
                pool.slots, pool.queue_depth = slots, queue_depth

    # ------------------------------------------------------------------ call limits
    def admit_call(self, agent_id: str, conversation_id: str, agent_limit: int,
                   conversation_limit: int) -> CallTicket:
        with self._lock:
            if self._agents.get(agent_id, 0) >= agent_limit:
                raise Overloaded(f"agent {agent_id} has {agent_limit} calls in flight",
                                 retry_after=self._retry_after_locked(None))
            if self._conversations.get(conversation_id, 0) >= conversation_limit:
                raise Overloaded(
                    f"conversation {conversation_id} has {conversation_limit} calls in flight",
                    retry_after=self._retry_after_locked(None))
            self._agents[agent_id] = self._agents.get(agent_id, 0) + 1
            self._conversations[conversation_id] = self._conversations.get(conversation_id, 0) + 1
        return CallTicket(self, agent_id, conversation_id)

    def _end_call(self, agent_id: str, conversation_id: str) -> None:
        with self._lock:
            for counts, key in ((self._agents, agent_id), (self._conversations, conversation_id)):
                left = counts[key] - 1
                if left:
                    counts[key] = left
                else:
                    del counts[key]

    # ------------------------------------------------------------------ slots
    def acquire(self, providers: Sequence[str], cancel: CancelToken | None = None,
                max_wait_s: float = 0.0) -> Slot:
        """Take a free slot on the first provider in order that has one.

        With none free and max_wait_s > 0, queue FIFO on the first provider with queue room
        and wait at most max_wait_s. Raise Overloaded when every queue is full or the wait
        ends, and ProviderCancelled when cancel fires while queued. Nothing was dispatched
        in either case.
        """
        if not providers:
            raise ValueError("no providers to acquire from")
        with self._lock:
            if cancel is not None and cancel.cancelled:
                raise ProviderCancelled(cancel.reason)
            for name in providers:
                pool = self._pools[name]
                if pool.in_flight < pool.slots and not pool.queue:
                    self._take_locked(pool)
                    return Slot(self, name, self._now())
            waiter = None
            if max_wait_s > 0:
                for name in providers:
                    pool = self._pools[name]
                    if len(pool.queue) < pool.queue_depth:
                        waiter = _Waiter(name)
                        pool.queue.append(waiter)
                        pool.peak_queue = max(pool.peak_queue, len(pool.queue))
                        break
            if waiter is None:
                raise Overloaded(f"no slot or queue room on {list(providers)}",
                                 retry_after=self._retry_after_locked(providers[0]))
        unsubscribe = cancel.on_cancel(waiter.event.set) if cancel is not None else None
        try:
            waiter.event.wait(max_wait_s)
        finally:
            if unsubscribe is not None:
                unsubscribe()
        with self._lock:
            pool = self._pools[waiter.provider]
            if waiter.granted:
                if cancel is not None and cancel.cancelled:
                    # The slot arrived with the cancel; hand it on rather than keep it.
                    self._release_locked(waiter.provider, None)
                    raise ProviderCancelled(cancel.reason)
                return Slot(self, waiter.provider, self._now())
            pool.queue.remove(waiter)
            if cancel is not None and cancel.cancelled:
                raise ProviderCancelled(cancel.reason)
            raise Overloaded(f"waited {max_wait_s}s for {waiter.provider}",
                             retry_after=self._retry_after_locked(waiter.provider))

    @staticmethod
    def _take_locked(pool: _Pool) -> None:
        pool.in_flight += 1
        pool.peak_in_flight = max(pool.peak_in_flight, pool.in_flight)

    def _release(self, provider: str, started: float) -> None:
        with self._lock:
            self._release_locked(provider, self._now() - started)

    def _release_locked(self, provider: str, latency: float | None) -> None:
        pool = self._pools[provider]
        if latency is not None:
            pool.latencies.append(latency)
        if pool.queue and pool.in_flight <= pool.slots:
            # Hand the slot to the head of the queue; in_flight stays the same.
            waiter = pool.queue.popleft()
            waiter.granted = True
            waiter.event.set()
            return
        pool.in_flight -= 1

    # ------------------------------------------------------------------ retry_after
    def _median_latency_locked(self, provider: str | None) -> float:
        if provider is not None:
            samples = list(self._pools[provider].latencies)
        else:
            samples = [x for p in self._pools.values() for x in p.latencies]
        return statistics.median(samples) if samples else self._default_latency

    def _retry_after_locked(self, provider: str | None) -> float:
        median = self._median_latency_locked(provider)
        if provider is None:
            return min(self._cap, median)
        pool = self._pools[provider]
        return min(self._cap, median * (len(pool.queue) + 1) / pool.slots)

    # ------------------------------------------------------------------ observation
    def stats(self) -> dict[str, PoolStats]:
        with self._lock:
            return {name: PoolStats(p.slots, p.in_flight, len(p.queue), p.peak_in_flight,
                                    p.peak_queue, p.queue_depth)
                    for name, p in self._pools.items()}

    def calls_in_flight(self) -> tuple[dict[str, int], dict[str, int]]:
        with self._lock:
            return dict(self._agents), dict(self._conversations)
