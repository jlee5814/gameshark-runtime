"""Provider independent inference types: capabilities, requests, chunks, results, errors.

Every value here is immutable. A request carries the full context and nothing about who
asks: no agent, conversation, responsibility, or persona field (R1). Results carry the
call_id and attempt of the request that produced them, so a result can never be mistaken
for another call's.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import ClassVar

from gameshark.errors import Cancelled

MAX_ATTEMPTS = 3


class Privacy(Enum):
    LOCAL_ONLY = "local_only"  # context may never leave the host
    CLOUD_OK = "cloud_ok"  # cloud allowed under the inference.cloud grant


class Locality(Enum):
    LOCAL = "local"
    CLOUD = "cloud"


class Capability(StrEnum):
    TOOLS = "tools"
    JSON_MODE = "json_mode"
    VISION = "vision"


@dataclass(frozen=True)
class Capabilities:
    model: str  # provider model name, display only
    locality: Locality
    context_window: int  # tokens, prompt plus output
    features: frozenset[Capability] = frozenset()
    max_concurrency: int = 1  # slots; config, not discovered
    price_in_micros: int = 0  # per 1k input tokens, 0 for local
    price_out_micros: int = 0  # per 1k output tokens

    def __post_init__(self) -> None:
        object.__setattr__(self, "features", frozenset(Capability(f) for f in self.features))
        if not self.model:
            raise ValueError("model must be named")
        if self.context_window <= 0:
            raise ValueError("context_window must be positive")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if self.price_in_micros < 0 or self.price_out_micros < 0:
            raise ValueError("prices must not be negative")

    def supports(self, needs: Iterable[Capability]) -> bool:
        return frozenset(needs) <= self.features


class Role(Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class ProviderRequest:
    call_id: str  # stable across retries and restarts
    attempt: int  # 1..MAX_ATTEMPTS, durable
    messages: tuple[Message, ...]  # full context; provider keeps no session
    max_tokens: int  # bounds cost and the budget reservation
    deadline: float  # absolute monotonic time
    privacy: Privacy = Privacy.LOCAL_ONLY
    temperature: float = 0.0
    needs: frozenset[Capability] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "needs", frozenset(Capability(n) for n in self.needs))
        if not self.call_id:
            raise ValueError("call_id must be set")
        if not 1 <= self.attempt <= MAX_ATTEMPTS:
            raise ValueError(f"attempt must be in 1..{MAX_ATTEMPTS}")
        if not self.messages:
            raise ValueError("a request carries its full context; messages is empty")
        if not all(isinstance(m, Message) for m in self.messages):
            raise TypeError("messages must be Message values")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")


@dataclass(frozen=True)
class Chunk:
    call_id: str
    index: int
    text: str


class FinishReason(Enum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"


@dataclass(frozen=True)
class Usage:
    tokens_in: int
    tokens_out: int


@dataclass(frozen=True)
class ProviderResult:
    call_id: str
    attempt: int
    text: str
    finish_reason: FinishReason
    usage: Usage
    model: str
    instance_ref: str | None = None  # per incarnation handle, audit only


class HealthStatus(Enum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


@dataclass(frozen=True)
class Health:
    status: HealthStatus
    detail: str = ""


# Error taxonomy. retryable says whether another attempt may succeed; before_dispatch says
# the error is raised before any request leaves the process, so it consumes no attempt and
# no budget.


class ProviderError(Exception):
    retryable: ClassVar[bool] = False
    before_dispatch: ClassVar[bool] = False


class ProviderUnavailable(ProviderError):
    retryable = True


class ModelCrashed(ProviderError):
    retryable = True


class Timeout(ProviderError):
    retryable = True


class RateLimited(ProviderError):
    retryable = True

    def __init__(self, message: str = "", retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Overloaded(ProviderError):
    retryable = True
    before_dispatch = True

    def __init__(self, message: str = "", retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class StreamInterrupted(ProviderError):
    retryable = True


class ContextConflict(ProviderError):
    retryable = True
    before_dispatch = True


class CapabilityUnsupported(ProviderError):
    before_dispatch = True


class ContextTooLong(ProviderError):
    before_dispatch = True


class BudgetExceeded(ProviderError):
    before_dispatch = True

    def __init__(self, message: str = "", reset_at: float = 0.0) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class EgressDenied(ProviderError):
    before_dispatch = True


class AuthFailed(ProviderError):
    pass


class InvalidRequest(ProviderError):
    pass


class AttemptsExhausted(ProviderError):
    pass


class ProviderCancelled(ProviderError, Cancelled):
    """Operator or drain cancellation reached a provider call. Terminal, never retried.

    Subclasses the runtime's Cancelled so drain code that catches Cancelled still does.
    """


class StreamNotFinished(RuntimeError):
    """result() was read before the call's stream ended. A caller bug, not a provider fault."""
