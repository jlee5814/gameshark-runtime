"""The provider contract: one stateless executor per model endpoint.

generate returns a ProviderCall that owns everything about one invocation: its chunks,
its usage, and its terminal error. The provider keeps no result slot of its own, so two
concurrent calls on one provider can never read each other's text, usage, or error.

    call = provider.generate(req, cancel)   # no I/O; raises only pre dispatch errors
    for chunk in call: ...                  # chunks of this call only
    res = call.result()                     # this call's result, or its terminal error

Providers never own identity, conversations, permissions, or responsibility state.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from gameshark.inference.types import (
    Capabilities,
    Chunk,
    Health,
    ProviderRequest,
    ProviderResult,
)
from gameshark.lifecycle import CancelToken


@runtime_checkable
class ProviderCall(Protocol):
    request: ProviderRequest

    def __iter__(self) -> Iterator[Chunk]: ...

    def result(self) -> ProviderResult:
        """Return this call's result once its stream ended.

        Re-raise the call's terminal ProviderError if it failed; raise StreamNotFinished if
        the stream has not ended. Partial text never appears in a result.
        """
        ...

    def close(self) -> None:
        """Abandon the stream. Idempotent."""
        ...


@runtime_checkable
class Provider(Protocol):
    name: str

    def capabilities(self) -> Capabilities: ...

    def health(self, timeout_s: float) -> Health: ...

    def generate(self, req: ProviderRequest, cancel: CancelToken) -> ProviderCall: ...


def complete(call: ProviderCall) -> ProviderResult:
    """Drain a call and return its result, or raise its terminal error."""
    try:
        for _ in call:
            pass
        return call.result()
    finally:
        call.close()
