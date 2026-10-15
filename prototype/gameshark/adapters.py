"""Adapters and capability context.

An adapter connects one agent to one external surface. It declares the capabilities it
needs. Authorization is durable (see authz.GrantStore): the supervisor records declared
caps on first registration without overriding a prior revocation, builds the context
from the effective grants, and gates every effect at dispatch time. An adapter reaches
external surfaces only through ctx.emit_effect, so every consequential action passes
through the effect journal and the authorization check.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from gameshark.effects import Effect


@dataclass(frozen=True)
class AdapterContext:
    agent_id: str
    identity_uuid: str
    caps: frozenset[str]                       # effective caps at context build time
    emit_effect: Callable[[Effect], str]       # gated path to a consequential action
    responsibilities: object                   # ResponsibilityStore, kept loose to avoid a cycle
    submit_event: Callable[[dict], str] | None = None   # feed a detected event into the inbox
    conn: object = None                        # sqlite connection for adapter owned durable state
    grants: object = None                      # GrantStore for per action authorization reads
    fenced: Callable[[Callable[[Any], Any]], Any] | None = None   # fenced durable write
    generation: int = 0                        # the owning lease generation
    clock: object = None                       # injectable Clock for durable timestamps
    admit: Callable[[], None] | None = None    # raises Draining once admission is frozen
    cancel: object = None                      # lifecycle CancelToken, checked at safe points
    inference: object = None                   # InferenceJournal for durable model calls
    presence: object = None                    # PresenceManager when presence is enabled


class Adapter(Protocol):
    name: str
    required_caps: frozenset[str]

    def init(self, ctx: AdapterContext) -> None: ...
    def teardown(self) -> None: ...
    def reconnect(self, ctx: AdapterContext) -> None: ...
    def on_event(self, event: dict) -> None: ...
    def build_effect(self, intent: dict) -> Effect: ...


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, Adapter] = {}

    def register(self, adapter: Adapter) -> None:
        self._adapters[adapter.name] = adapter

    def open(self, name: str, make_ctx: Callable[[str], AdapterContext]) -> None:
        self._adapters[name].init(make_ctx(name))

    def reconnect(self, name: str, make_ctx: Callable[[str], AdapterContext]) -> None:
        """Reopen a session. Identity and grant are reused, never reset (I1, I6)."""
        self._adapters[name].reconnect(make_ctx(name))

    def teardown_all(self) -> None:
        for a in self._adapters.values():
            a.teardown()

    def get(self, name: str) -> Adapter:
        return self._adapters[name]

    def names(self) -> list[str]:
        return list(self._adapters)
