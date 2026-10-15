"""A provider wrapper that timestamps each call and changes nothing else.

It sits under the router like any provider: capabilities, health, fingerprint, chunks,
results, and errors pass through unmodified. Each call emits JSON records: dispatch,
first chunk, cancel seen, end (finish, usage, structure), and error. Text is recorded
only when the caller allows it (local synthetic prompts); never for cloud.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator

from gameshark.inference.provider import Provider, ProviderCall
from gameshark.inference.types import (
    Capabilities,
    Chunk,
    Health,
    ProviderRequest,
    ProviderResult,
)
from gameshark.lifecycle import CancelToken

MARKERS = ("<think>", "</think>")
PREFIX = 160

Emit = Callable[[dict], None]


class ObservedProvider:
    def __init__(self, inner: Provider, emit: Emit, record_text: bool = False) -> None:
        self.inner = inner
        self.name = inner.name
        self.emit = emit
        self.record_text = record_text
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.dispatched = 0

    def capabilities(self) -> Capabilities:
        return self.inner.capabilities()

    def health(self, timeout_s: float) -> Health:
        return self.inner.health(timeout_s)

    def generate(self, req: ProviderRequest, cancel: CancelToken) -> ProviderCall:
        return ObservedCall(self, self.inner.generate(req, cancel), cancel)

    def _enter(self) -> int:
        with self._lock:
            self.active += 1
            self.dispatched += 1
            self.peak = max(self.peak, self.active)
            return self.active

    def _leave(self) -> None:
        with self._lock:
            self.active -= 1


class ObservedCall:
    def __init__(self, owner: ObservedProvider, inner: ProviderCall, cancel: CancelToken) -> None:
        self.owner = owner
        self.inner = inner
        self.request = inner.request
        self.t0 = 0.0
        self.first = 0.0
        self.cancel_at = 0.0
        self.chunks = 0
        self.head: list[str] = []
        self._live = False
        self._unsubscribe = cancel.on_cancel(self._cancelled)

    def _cancelled(self) -> None:
        self.cancel_at = time.monotonic()
        self._record("cancel_seen", since_dispatch_s=self._since(self.cancel_at))

    def _since(self, t: float) -> float | None:
        return round(t - self.t0, 4) if self.t0 and t else None

    def _record(self, kind: str, **fields) -> None:
        self.owner.emit({"obs": kind, "call_id": self.request.call_id,
                         "attempt": self.request.attempt, "t": round(time.time(), 4),
                         **fields})

    def _done(self) -> None:
        if self._live:
            self._live = False
            self.owner._leave()

    def __iter__(self) -> Iterator[Chunk]:
        self.t0 = time.monotonic()
        self._live = True
        active = self.owner._enter()
        self._record("dispatch", active=active, max_tokens=self.request.max_tokens,
                     messages=len(self.request.messages))
        try:
            for c in self.inner:
                if not self.chunks:
                    self.first = time.monotonic()
                    self._record("first_chunk", ttft_s=self._since(self.first))
                self.chunks += 1
                if self.owner.record_text and len(self.head) < 4:
                    self.head.append(c.text)
                yield c
        except BaseException as exc:
            now = time.monotonic()
            self._record("error", error=type(exc).__name__, elapsed_s=self._since(now),
                         chunks=self.chunks,
                         cancel_latency_s=round(now - self.cancel_at, 4)
                         if self.cancel_at else None)
            self._done()
            raise

    def result(self) -> ProviderResult:
        res = self.inner.result()
        end = time.monotonic()
        usage = res.usage
        fields = {"finish": res.finish_reason.value, "chunks": self.chunks,
                  "ttft_s": self._since(self.first), "total_s": self._since(end),
                  "usage": None if usage is None else
                  {"tokens_in": usage.tokens_in, "tokens_out": usage.tokens_out},
                  "text_chars": len(res.text),
                  "markers": [m for m in MARKERS if m in res.text],
                  "model": res.model}
        if self.owner.record_text:
            fields["head_chunks"] = self.head
            fields["text_prefix"] = res.text[:PREFIX]
        self._record("end", **fields)
        self._done()
        return res

    def close(self) -> None:
        self._unsubscribe()
        self._done()
        self.inner.close()
