"""Deterministic in process provider for tests and fault injection.

Output is a pure function of (provider name, call_id, messages), so a rerun of the same
request yields the same text. Scripts override text, add per chunk delays, and inject a
typed fault before a chosen chunk index, keyed by (call_id, attempt) or by call_id.

Each FakeCall owns its own stream state and result. The provider's counters exist only so
replay tests can assert how often it was invoked; nothing reads them to build a result.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from gameshark.inference.types import (
    Capabilities,
    CapabilityUnsupported,
    Chunk,
    ContextTooLong,
    FinishReason,
    Health,
    HealthStatus,
    Locality,
    ProviderCancelled,
    ProviderError,
    ProviderRequest,
    ProviderResult,
    StreamNotFinished,
    Timeout,
    Usage,
    estimate_tokens,
)
from gameshark.lifecycle import CancelToken

DEFAULT_WORDS = 8
HEALTHY = Health(HealthStatus.OK)


@dataclass(frozen=True)
class Fault:
    """Raise error() before chunk at_chunk is emitted, so exactly at_chunk chunks arrive."""

    at_chunk: int
    error: Callable[[], ProviderError]


@dataclass(frozen=True)
class Script:
    chunks: tuple[str, ...] | None = None  # None: deterministic default text
    delays: tuple[float, ...] = ()  # seconds before chunk i; missing entries are 0
    fault: Fault | None = None
    finish_reason: FinishReason = FinishReason.STOP
    usage: Usage | None = None  # reported usage override, e.g. a tokenizer above the estimate


def _wait_on_token(seconds: float, token: CancelToken) -> None:
    token.wait(seconds)


class FakeProvider:
    def __init__(
        self,
        name: str = "fake",
        capabilities: Capabilities | None = None,
        scripts: Mapping[tuple[str, int] | str, Script] | None = None,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float, CancelToken], None] = _wait_on_token,
        health: Health = HEALTHY,
        health_script: Sequence[Health] = (),
        on_health: Callable[[float], None] | None = None,
    ) -> None:
        self.name = name
        self._caps = capabilities or Capabilities(
            model=f"{name}-model", locality=Locality.LOCAL, context_window=4096
        )
        self._scripts = dict(scripts or {})
        self.now = now
        self.sleep = sleep
        self.health_status = health
        self._health_script = list(health_script)  # one per probe; then health_status
        self.on_health = on_health  # called with the probe timeout; may block or raise
        self.health_timeouts: list[float] = []
        self._lock = threading.Lock()
        self._invocations: list[ProviderRequest] = []
        self.health_checks = 0
        self.in_flight = 0
        self.peak_in_flight = 0

    def capabilities(self) -> Capabilities:
        return self._caps

    def health(self, timeout_s: float) -> Health:
        with self._lock:
            self.health_checks += 1
            self.health_timeouts.append(timeout_s)
            scripted = self._health_script.pop(0) if self._health_script else None
        if self.on_health is not None:
            self.on_health(timeout_s)
        return scripted or self.health_status

    def generate(self, req: ProviderRequest, cancel: CancelToken) -> FakeCall:
        if not self._caps.supports(req.needs):
            missing = sorted(req.needs - self._caps.features)
            raise CapabilityUnsupported(f"{self.name} lacks {missing}")
        if estimate_tokens(req) + req.max_tokens > self._caps.context_window:
            raise ContextTooLong(f"{self.name} window is {self._caps.context_window}")
        script = self._scripts.get((req.call_id, req.attempt)) or self._scripts.get(req.call_id)
        with self._lock:
            self._invocations.append(req)
        return FakeCall(self, req, cancel, script or Script())

    # Observation for replay tests.

    @property
    def invocations(self) -> tuple[ProviderRequest, ...]:
        with self._lock:
            return tuple(self._invocations)

    def count(self, call_id: str | None = None, attempt: int | None = None) -> int:
        return sum(
            1
            for r in self.invocations
            if (call_id is None or r.call_id == call_id)
            and (attempt is None or r.attempt == attempt)
        )

    def default_text(self, req: ProviderRequest) -> tuple[str, ...]:
        h = hashlib.sha256(self.name.encode())
        h.update(req.call_id.encode())
        for m in req.messages:
            h.update(f"\0{m.role.value}\0{m.content}".encode())
        hexd = h.hexdigest()
        words = [f"w{hexd[i * 4:i * 4 + 4]}" for i in range(DEFAULT_WORDS)]
        return tuple(w + " " for w in words[:-1]) + (words[-1],)

    def _enter(self) -> None:
        with self._lock:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)

    def _exit(self) -> None:
        with self._lock:
            self.in_flight -= 1


class FakeCall:
    def __init__(
        self, provider: FakeProvider, req: ProviderRequest, cancel: CancelToken, script: Script
    ) -> None:
        self.request = req
        self._provider = provider
        self._cancel = cancel
        self._script = script
        self._result: ProviderResult | None = None
        self._error: ProviderError | None = None
        self._stream = self._run()

    def __iter__(self) -> Iterator[Chunk]:
        return self._stream

    def result(self) -> ProviderResult:
        if self._error is not None:
            raise self._error
        if self._result is None:
            raise StreamNotFinished(f"call {self.request.call_id} has not finished streaming")
        return self._result

    def close(self) -> None:
        self._stream.close()

    def _checkpoint(self) -> None:
        # Operator intent wins over the deadline: a cancelled call is never retried.
        if self._cancel.cancelled:
            raise ProviderCancelled(self._cancel.reason)
        if self._provider.now() >= self.request.deadline:
            raise Timeout(f"call {self.request.call_id} passed its deadline")

    def _run(self) -> Iterator[Chunk]:
        req, script, provider = self.request, self._script, self._provider
        parts = script.chunks if script.chunks is not None else provider.default_text(req)
        finish = script.finish_reason
        if len(parts) > req.max_tokens:
            parts, finish = parts[: req.max_tokens], FinishReason.LENGTH
        fault = script.fault
        provider._enter()
        try:
            emitted: list[str] = []
            for i in range(len(parts) + 1):
                self._checkpoint()
                if fault is not None and fault.at_chunk == i:
                    raise fault.error()
                if i == len(parts):
                    break
                delay = script.delays[i] if i < len(script.delays) else 0.0
                if delay > 0:
                    provider.sleep(delay, self._cancel)
                    self._checkpoint()
                emitted.append(parts[i])
                yield Chunk(req.call_id, i, parts[i])
            self._result = ProviderResult(
                call_id=req.call_id,
                attempt=req.attempt,
                text="".join(emitted),
                finish_reason=finish,
                usage=script.usage or Usage(tokens_in=estimate_tokens(req),
                                            tokens_out=len(emitted)),
                model=provider.capabilities().model,
                instance_ref=f"{provider.name}#1",
            )
        except ProviderError as exc:
            self._error = exc
            raise
        finally:
            provider._exit()
