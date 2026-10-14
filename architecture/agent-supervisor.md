# GameShark agent supervisor: minimal design

Clean room design. No Discord code is copied. The shapes borrow the observed Discord patterns in `../research/discord/deep-analysis.md`: durable commit record, checkpoint before effect, idempotency sentinel, stage then relaunch, watchdog restart. Illustrative Python below, not wired into any existing project. Stack idiom: uv, ruff, pytest.

## Goals

A minimal supervisor that provides:

- persistent agent identity
- adapter init and teardown
- event routing
- durable task checkpoints
- interruption and restart recovery
- delivery at least once with idempotency protection, yielding exactly once consequence
- explicit authorization boundaries per adapter

Opportunity cost note: this design buys exactly once consequential behavior at the price of one durable write before and after every external effect. For effects that are already idempotent at the destination, the journal can be skipped, trading safety for latency. Default to the journal; opt out per effect only with proof the destination deduplicates.

## Durable store contract

One small durable store, authoritative over all resume truth. Memory is a cache of it. A single writer per agent, enforced by the ownership lease. Minimal interface:

```python
from typing import Protocol, Optional

class DurableStore(Protocol):
    def read_identity(self, agent_id: str) -> Optional[dict]: ...
    def acquire_lease(self, agent_id: str, owner_token: str, ttl_s: int) -> bool: ...
    def release_lease(self, agent_id: str, owner_token: str) -> None: ...
    def put_journal(self, key: str, row: dict) -> None: ...     # atomic upsert
    def get_journal(self, key: str) -> Optional[dict]: ...
    def pending_journal(self, agent_id: str) -> list[dict]: ... # status == "pending"
    def put_checkpoint(self, task_id: str, step: str) -> None: ...
    def get_checkpoint(self, task_id: str) -> Optional[str]: ...
```

A file backed implementation using atomic rename (write temp, fsync, rename) satisfies this on one host. The rename gives the atomic single commit Discord gets from rewriting `installed.json`.

## Identity

Assigned once, reloaded forever. Secret material stays in the OS keychain, mirroring Discord's `com.hnc.agekeys` observation. The identity record holds no secrets.

```python
import uuid

def load_or_create_identity(store: DurableStore, name: str) -> dict:
    existing = store.read_identity(name)
    if existing is not None:
        return existing                     # never regenerate on restart
    identity = {"agent_id": name, "uuid": str(uuid.uuid4()), "created": now_iso()}
    store.put_journal(f"identity:{name}", {"status": "done", "identity": identity})
    return identity
```

## Ownership lease

Guarantees a single accountable owner. A second process cannot claim Stark's responsibilities while the lease holds. On restart the same agent reacquires after the prior lease expires or is released.

```python
def claim(store: DurableStore, agent_id: str, owner_token: str) -> bool:
    return store.acquire_lease(agent_id, owner_token, ttl_s=30)
```

## Effect journal: the exactly once core

Every consequential action is wrapped. Intent is journaled before the effect, outcome after. Recovery reconciles pending rows by asking the destination whether the effect already landed, keyed by the idempotency key.

```python
from dataclasses import dataclass
from typing import Callable

@dataclass
class Effect:
    key: str                      # idempotency key, stable per logical action
    do: Callable[[int], str]      # performs the external effect under a fence, returns id
    probe: Callable[[], ProbeResult]  # returns FOUND(id), ABSENT, or UNKNOWN

def perform_once(store: DurableStore, agent_id: str, eff: Effect, fence: int) -> str:
    prior = store.get_journal(eff.key)
    if prior and prior["status"] == "done":
        return prior["external_id"]                     # already done, suppress duplicate

    # checkpoint before effect
    store.put_journal(eff.key, {"agent_id": agent_id, "status": "pending"})

    # reconcile: never read a missing result as proof the effect did not run.
    result = eff.probe()
    if result.kind is FOUND:
        external_id = result.external_id                # confirmed landed, adopt its id
    elif result.kind is UNKNOWN:
        store.put_journal(eff.key, {"agent_id": agent_id, "status": "blocked"})
        raise AmbiguousEffect(eff.key)                  # refuse to guess, escalate
    else:  # ABSENT, trustworthy only under a consistent authoritative destination
        external_id = eff.do(fence)                     # safe because the destination dedups by key

    # checkpoint outcome
    store.put_journal(eff.key, {"agent_id": agent_id, "status": "done",
                                "external_id": external_id})
    return external_id
```

The three valued `probe` is what makes delivery at least once safe. A crash between
`put_journal(pending)` and `put_journal(done)` leaves a pending row; on restart
`perform_once` runs again. `FOUND` adopts the landed id and fires no duplicate. `ABSENT`
dispatches again, safe only because the destination deduplicates by the operation key.
`UNKNOWN` is never guessed: the effect blocks and the responsibility escalates. A missing
result is never read as proof the action did not run. Local fencing guards the local
write; preventing a stale worker's external effect additionally requires the destination
to enforce the fence. Working implementation: `../prototype/gameshark/effects.py`.

## Adapters and authorization boundary

An adapter declares the capabilities it needs. The supervisor grants a frozen capability set at init. Recovery reuses the same grant and never widens it, matching Discord's fixed preload channel boundary.

```python
class Adapter(Protocol):
    name: str
    required_caps: frozenset[str]
    def init(self, ctx: "AdapterContext") -> None: ...
    def teardown(self) -> None: ...
    def on_event(self, event: dict) -> None: ...

@dataclass(frozen=True)
class AdapterContext:
    agent_id: str
    caps: frozenset[str]
    emit_effect: Callable[[Effect], str]   # the only path to a consequential action

def open_adapter(a: Adapter, agent_id: str, granted: frozenset[str],
                 run_effect: Callable[[Effect], str]) -> AdapterContext:
    if not a.required_caps <= granted:
        raise PermissionError(f"{a.name} requests caps beyond its grant")
    ctx = AdapterContext(agent_id, granted & a.required_caps, run_effect)
    a.init(ctx)
    return ctx
```

An adapter reaches external surfaces only through `ctx.emit_effect`, so every consequential action passes through the journal. An adapter cannot bypass the boundary to double act.

## Event router

Central, like Discord's main process bus. It fans events to adapters and is the only component that calls `perform_once`.

```python
class Supervisor:
    def __init__(self, store, agent_id, owner_token):
        self.store = store
        self.identity = load_or_create_identity(store, agent_id)
        self.adapters: dict[str, tuple[Adapter, AdapterContext]] = {}
        self.owner_token = owner_token

    def _run_effect(self, agent_id: str):
        return lambda eff: perform_once(self.store, agent_id, eff)

    def register(self, a: Adapter, granted: frozenset[str]) -> None:
        ctx = open_adapter(a, self.identity["agent_id"], granted,
                           self._run_effect(self.identity["agent_id"]))
        self.adapters[a.name] = (a, ctx)

    def start(self) -> None:
        if not claim(self.store, self.identity["agent_id"], self.owner_token):
            raise RuntimeError("another owner holds the lease")
        self.recover()                      # replay before accepting events

    def recover(self) -> None:
        # Reconcile every pending effect through the owning adapter, then replay the
        # durable event inbox. Real implementation in prototype/gameshark/supervisor.py.
        for row in self.store.pending_journal(self.identity["agent_id"]):
            adapter = self.adapters[row["adapter"]][0]
            eff = adapter.build_effect(row["intent"])
            self.journal.reconcile(eff)         # probe: FOUND -> done, ABSENT -> redo, UNKNOWN -> block
        self.router.replay()                    # redeliver unacknowledged events, deduped by id

    def route(self, event: dict) -> None:
        target = self.adapters.get(event["adapter"])
        if target:
            target[0].on_event(event)

    def stop(self) -> None:
        for a, _ in self.adapters.values():
            a.teardown()
        self.store.release_lease(self.identity["agent_id"], self.owner_token)
```

## The Stark scenario

One agent instance, Stark, holds two responsibilities at once:

1. a simulated game responsibility (make exactly one move per turn), and
2. monitoring a mock GitHub PR (post exactly one review comment when the PR reaches a trigger state).

Both are consequential: a double game move corrupts the match, a double PR comment spams the author.

Adapters:

- `GameAdapter` (caps `{"game.move"}`): on a `turn` event, emits an effect keyed `game:{match}:{turn}` whose `do` submits the move and whose `probe` reads the match log for a move already recorded at that turn.
- `PrAdapter` (caps `{"pr.comment"}`): on a `pr_state` event reaching the trigger, emits an effect keyed `pr:{repo}:{pr}:{trigger}` whose `do` posts the comment and whose `probe` lists existing comments for a matching marker.

Flow:

1. Stark starts, loads identity, acquires the lease, opens both adapters.
2. A `turn` event arrives. `GameAdapter` journals `game:m1:7` pending, submits the move, gets move id, journals done.
3. A `pr_state` event arrives at the trigger. `PrAdapter` journals `pr:acme/widget:42:needs_review` pending. **The process is killed here, after the comment call but before the outcome journal.**
4. The supervisor restarts. Same identity Stark, reacquires lease, replays. It finds `pr:...:needs_review` pending.
5. `perform_once` reruns: `probe` lists PR comments, finds the comment already landed, journals done with its id. **No second comment posts.**
6. Stark resumes both responsibilities. The next `turn` event produces exactly one new move; the PR responsibility is complete and not retriggered.

Process persistence played no role. Identity persistence kept Stark the same owner. Task persistence kept the PR step resumable. Durable responsibility ownership plus the probe guard delivered exactly once consequence.

## Acceptance test spec (continuity)

Implementable later as a pytest. Names the crash point, the idempotency key, and the exactly once assertion.

```
Feature: Stark resumes dual responsibilities without duplicate consequential action

Given a DurableStore on a temp path
  And a Supervisor for agent "stark" holding GameAdapter and PrAdapter
  And a mock game surface that records moves in a list
  And a mock GitHub surface that records PR comments in a list

Scenario: crash after external effect, before outcome journal
  Given a turn event for match "m1" turn 7 has been processed to done
   When a pr_state event drives PrAdapter to journal key "pr:acme/widget:42:needs_review" as pending
    And the PR comment call has returned (comment exists on the mock surface)
    And the process is killed before the outcome journal is written
   Then exactly one PR comment exists on the mock surface
    And the journal row for that key has status "pending"

  When a new Supervisor for agent "stark" starts on the same DurableStore
   Then it reacquires the ownership lease as the same identity uuid
    And recovery finds the pending key "pr:acme/widget:42:needs_review"
    And perform_once probes the PR surface, sees the landed comment, and marks the row done
   Then exactly one PR comment still exists   # no duplicate
    And the journal row status is "done" with the external comment id

  When a new turn event for match "m1" turn 8 arrives
   Then exactly one new game move is recorded for turn 8
    And no second move is recorded for turn 7

Invariant checks (assert across the run):
  - the agent uuid before and after restart is identical
  - no consequential effect key reaches status "done" twice
  - every "done" row has a non null external id
  - PrAdapter never acted outside caps {"pr.comment"}
```

The decisive assertions: exactly one PR comment after a crash placed between effect and outcome journal, and a stable identity uuid across restart. Those two prove durable responsibility ownership rather than mere process or session persistence.

## What this design deliberately omits

- Multi host leases (needs a real lease service, not a file). Stated as future work.
- Adapter code hot swap (the Discord stage then relaunch would map to restart the worker with a new adapter version). Out of scope for the minimal supervisor.
- Encryption of the durable store. Secrets stay in the keychain; the store holds references only.
