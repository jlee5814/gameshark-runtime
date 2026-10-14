# GameShark inference router: v0.3 design

Status: design, with steps 0 and 1 of Minimal v0.3 scope implemented (graceful drain; provider contracts in `prototype/gameshark/inference/`). v0.1 and v0.2 runtime code stays unchanged.

Stark gains inference without letting any model own Stark. Providers become disposable executors behind one contract; identity, memory, and responsibilities stay in GameShark's SQLite store under the existing fenced lease. The Bionic (LM Studio) study supplies the problem shape (`../research/bionic/deep-analysis.md`); GameShark code stays clean room.

## The answer first

The smallest router that lets Stark switch between local and cloud models while keeping identity, memory, responsibilities, and recovery has four parts:

1. **One stateless provider contract.** A request carries the full context; a provider returns a stream and a usage record and keeps nothing GameShark depends on. One OpenAI compatible HTTP transport reaches SGLang, `mlx_lm.server`, the LM Studio and Bionic local server, and most cloud APIs. Switching models changes a routing decision, never Stark's state.
2. **A router that filters, orders, and admits.** Capability match, then policy (privacy class and a durable cloud egress grant), then local first ordering, then health, then a budget reservation and a bounded slot. It refuses with a typed error rather than queueing without bound.
3. **An inference journal in the existing store.** Conversations and messages belong to the agent identity. Each call has a stable `call_id`; its result commits once, in one fenced transaction, beside the message it produces. Execution is at least once; commit is at most once. Effects caused by model output still pass through the v0.1 effect journal.
4. **A drain then fence handoff.** Freeze admission, finish or cancel in flight calls inside a deadline, release the lease, and let the successor's higher generation reject every late commit. Outstanding work resumes because its owner (an inbox event or a responsibility step) replays and reissues the same `call_id`.

Everything else (engine process ownership, KV aware routing, multi host) is deferred.

## Evidence tags

Claims drawn from Bionic or vendor documentation carry one tag:

- **OBSERVED**: read statically from a shipped Bionic artifact. Never executed; not runtime verified.
- **DOCUMENTED**: stated in the vendor's public documentation, fetched 2026-10-14.
- **INFERRED**: derived from observed code or documentation, not confirmed.
- **UNKNOWN**: not resolvable from available evidence.

Statements about GameShark itself are design requirements, not evidence.

| Source fact | Tag | Design consequence |
|---|---|---|
| Loaded model instances live in memory only; restart rebuilds them on demand (`modelInstancesSignal`) | OBSERVED | Never persist instance state; persist context and intent only |
| A crashed model process is marked `crashed` and never restarted; the next request reloads under a per model mutex (`getOrLoadInstanceReference`, `ngManagedLoadMutex`) | OBSERVED | Crash is a state; rematerialize at the demand edge |
| Unload during prediction maps to `NGRetryableError` unless the unload was explicit (`isExplicitModelUnloadError`) | OBSERVED | Retryable versus terminal split; operator intent never retried |
| Resource fit is planned before any process exists; rejection is typed (`ModelLoadGuardrailPlanner`, `ModelLoadingGuardrailRejectionError`) | OBSERVED | Admission before dispatch with typed refusal |
| Startup retries stop at 3 and each must carry a changed plan (`getStartupRetryPlan`, `recoveryOriginalValues`) | OBSERVED | Bounded retries that name their change; record degradation |
| Per instance admission `max(1, numParallelSessions)`; vLLM admits unbounded and batches itself (`ParallelPredictionQueue`) | OBSERVED | Per provider slot count is configuration, not discovery |
| Local inference ownership is a held SQLite write transaction with a `flushing` drain phase (`LocalInferenceHandoff`) | OBSERVED | Drain before release; GameShark adds a fence generation |
| JIT loaded models default to a 60 minute idle TTL; the timer resets on each request; `ttl` is settable per request | DOCUMENTED (LM Studio, Idle TTL and Auto Evict) | Idle TTL governs residency only |
| Auto evict keeps at most one JIT loaded model resident and leaves explicitly loaded models alone | DOCUMENTED (LM Studio) | Matches observed `unloadPreviousJITModelOnLoad`; a local LM Studio slot can vanish when another app triggers a JIT load |
| LM Studio serves `GET /v1/models`, `POST /v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/responses`, `/v1/decisions`; examples use port 1234 | DOCUMENTED (LM Studio, OpenAI Compatibility Endpoints) | One OpenAI compatible transport covers it |
| SGLang implements the OpenAI API with streaming; examples use port 30000 | DOCUMENTED (SGLang, OpenAI APIs Completions) | Same transport |
| `mlx_lm.server` serves `/v1/chat/completions` and `/v1/models`, streams `chat.completion.chunk`, defaults to localhost:8080, and is not recommended for production | DOCUMENTED (mlx_lm SERVER.md) | Same transport; treat as single slot and loopback only |
| Behavior of Bionic or LM Studio under concurrent external load | UNKNOWN | Slot counts come from GameShark config and live tests, never from the Bionic study |
| Whether closing an HTTP stream aborts generation on each server | UNKNOWN | Cancellation charges the full reservation; verify per provider in live smoke |

## Architecture

```
 inbox event / responsibility step (v0.1, v0.2)
        │ handler asks: complete(call_id, conversation, needs)
        ▼
 ┌──────────────────────── stark run (one process, flock, fenced lease) ──────────────────────┐
 │ tick thread (owns the SQLite writer connection)                                             │
 │   InferenceJournal   lookup call_id ─ done? return committed message (replay short circuit) │
 │        │ no                                                                                 │
 │   InferenceRouter                                                                           │
 │     1 capability filter   context window, tools, json, vision                               │
 │     2 policy filter       privacy class, inference.cloud grant (GrantStore, I6)             │
 │     3 ordering            agent preference, else local first                                │
 │     4 health filter       ModelTracker state, circuit cooldown                              │
 │     5 admission           budget reservation (fenced write), slot or bounded queue          │
 │        │ dispatch                                     ▲ completion queue                    │
 │        ▼                                              │                                     │
 │   executor threads (bounded) ── Provider.generate ────┘  commit on tick thread (fenced)     │
 │        │                                                                                    │
 │   ModelTracker (memory only): discovered … ready ⇄ executing … crashed / failed             │
 └────────┼────────────────────────────────────────────────────────────────────────────────────┘
          │ OpenAI compatible HTTP (stdlib)            │ FakeProvider (tests, fault injection)
   ┌──────┴────────┬───────────────┬────────────────┐
 SGLang      mlx_lm.server   LM Studio/Bionic    cloud API
 (local)       (local)       server (local)    (egress grant)
                                                     │
            runtime.db: conversations, messages, inference_calls, budget_ledger, agent_budgets
```

Separation rules, carried from v0.2: providers never touch storage; the router never owns identity; the journal knows nothing of HTTP; only the tick thread writes SQLite.

**Why executor threads.** `run_forever` (`prototype/gameshark/worker.py`) is single threaded and renews the lease every third of its TTL (30 s default). An inference call can outlast the TTL, so a blocking call on the tick thread would let the lease expire and fence Stark mid answer. Calls run on a bounded executor; results return through an in memory completion queue; the tick thread commits them. A lost result costs one rerun, never a torn write.

## Provider interface

```python
class Privacy(Enum):
    LOCAL_ONLY = "local_only"      # context may never leave the host
    CLOUD_OK = "cloud_ok"          # cloud allowed under the inference.cloud grant

@dataclass(frozen=True)
class Capabilities:
    model: str                     # provider model name, display only
    locality: Locality             # LOCAL | CLOUD
    context_window: int            # tokens, prompt plus output
    features: frozenset[Capability]  # TOOLS, JSON_MODE, VISION
    max_concurrency: int           # slots; config, not discovered
    price_in_micros: int           # per 1k input tokens, 0 for local
    price_out_micros: int          # per 1k output tokens

@dataclass(frozen=True)
class ProviderRequest:
    call_id: str                   # stable across retries and restarts
    attempt: int                   # 1..3, durable
    messages: tuple[Message, ...]  # full context; provider keeps no session
    max_tokens: int                # bounds cost and the budget reservation
    deadline: float                # absolute monotonic time
    privacy: Privacy
    temperature: float
    needs: frozenset[Capability]   # required capabilities

@dataclass(frozen=True)
class Chunk:
    call_id: str                   # the call this chunk belongs to
    index: int
    text: str

@dataclass(frozen=True)
class ProviderResult:
    call_id: str                   # echoed from the request
    attempt: int
    text: str
    finish_reason: FinishReason    # STOP | LENGTH | TOOL_CALLS
    usage: Usage                   # tokens_in, tokens_out
    model: str
    instance_ref: str | None       # per incarnation handle, audit only

class ProviderCall(Protocol):      # one invocation; owns its stream, usage, and error
    request: ProviderRequest
    def __iter__(self) -> Iterator[Chunk]: ...
    def result(self) -> ProviderResult: ...   # this call only; re-raises its terminal error
    def close(self) -> None: ...

class Provider(Protocol):
    name: str
    def capabilities(self) -> Capabilities: ...
    def health(self, timeout_s: float) -> Health: ...          # cheap: GET /v1/models
    def generate(self, req: ProviderRequest, cancel: CancelToken) -> ProviderCall: ...
```

Contracts:

- **Request scoped results.** Revised in step 1. The first draft put `result()` on the provider, valid after the iterator ended. Two concurrent calls on one provider (SGLang runs 8 slots) would then share one result slot and could read each other's text, usage, or error. `generate` now returns a `ProviderCall` handle that owns its stream, usage, and terminal error; the provider keeps no result state. `result()` before the stream ends raises `StreamNotFinished`; after a fault it re-raises that call's error. Results and chunks carry `call_id`, so the journal can reject a mismatched commit.
- **Pre dispatch refusal.** `generate` does no I/O. It raises only `CapabilityUnsupported`, `ContextTooLong`, or `InvalidRequest`; transport and model errors surface from iteration.
- **Statelessness.** A provider may cache prefixes or KV state; GameShark never relies on it. Every request is complete. Correctness under a cold cache is a test requirement.
- **Streaming.** Chunks are internal. No chunk reaches a user or external surface before commit (R5). Live streaming to a user is deferred.
- **Cancellation.** `CancelToken` is cooperative. On cancel or deadline, the provider closes the stream within 2 s grace and raises `ProviderCancelled` (a subclass of the runtime's existing `Cancelled`) or `Timeout`; cancel wins when both apply. Partial text is discarded, never committed. Server side abort on disconnect is UNKNOWN per provider, so the ledger charges the full reservation.
- **Errors.** Providers raise only taxonomy types below; raw transport exceptions are wrapped at the boundary, as `RestGitHubClient` does in v0.2.
- **Secrets.** API keys come from the environment or keychain at construction; they never enter `ProviderRequest`, the journal, or logs.

v0.3 ships two providers: `FakeProvider` (deterministic text, scripted faults at each chunk index, scripted latency) and `OpenAICompatProvider` (stdlib `http.client`, SSE parsing, `/v1/chat/completions` and `/v1/models`, base URL plus optional key). Locality is configuration: loopback URLs are local; anything else is cloud. A loopback URL never needs the egress grant; a cloud URL always does.

## Router

### Selection

Input: `needs`, conversation privacy, estimated prompt tokens, `max_tokens`. Output: an ordered candidate list, or a typed refusal.

1. **Capability filter.** Keep models whose capabilities cover `needs` and whose `context_window >= prompt_tokens + max_tokens`. Empty set: `CapabilityUnsupported` or `ContextTooLong`.
2. **Policy filter.** Drop cloud candidates when privacy is `LOCAL_ONLY` or the durable grant `inference.cloud` is not effective. If only cloud candidates remained: `EgressDenied`.
3. **Ordering.** The agent's configured preference list if present; else local first, cheapest cloud second.
4. **Health filter.** Drop models in `failed` cooldown or with an open circuit after `RateLimited`. Keep `crashed` and `unloaded`; they rematerialize lazily.
5. **Admission.** Reserve budget, then take a slot or a queue position on the first candidate that has one.

On a retryable error the router moves to the next candidate. A move counts as an attempt and is a changed plan; the same candidate is retried only after its state changed (crashed instance rebuilt, `retry_after` elapsed). The committed call records `degraded = 1` when the answering model differs from the first candidate, so audit can tell a fallback answer from the requested one (OBSERVED lesson from `recoveryOriginalValues`).

### Budgets and admission

`agent_budgets` sets, per agent and window, `max_tokens` and `max_cost_micros`. Admission writes a `budget_ledger` row with status `reserved` before dispatch:

- reserved tokens = prompt tokens + `max_tokens`
- reserved cost = prices × reserved tokens (zero for local)

Admission succeeds only if settled plus reserved spend in the window, plus this reservation, stays within limits. Output tokens cannot exceed `max_tokens`, so actual spend never exceeds the reservation and the budget never overruns (R6). On completion the row settles to actual usage; on crash or cancel it settles to the full reservation (`lost`). Spend counts attempts, not commits: cloud providers bill a discarded answer.

### Concurrency, queueing, backpressure

| Limit | Default | When full |
|---|---|---|
| Slots per provider | `max_concurrency` (SGLang 8, MLX 1, LM Studio 1, cloud 4; config) | enqueue |
| Queue depth per provider | 16 | `Overloaded(retry_after)` |
| In flight per agent | 4 | `Overloaded(retry_after)` |
| In flight per conversation | 1 | caller waits for the prior call's commit |
| Executor threads | sum of slots | never exceeded; slots bound it |

Queues are FIFO per provider. A full queue fails fast rather than blocking the tick thread. The caller does not spin: an inbox event stays pending and replays; a responsibility sets `next_due = now + retry_after`, reusing the v0.2 scheduler. `retry_after` estimates queue depth × median call latency, capped at 60 s.

The per conversation limit keeps context linear: a call snapshots `base_seq`, and its commit requires `conversations.next_seq == base_seq`; a mismatch raises `ContextConflict`, and the caller reissues with fresh context.

## Model lifecycle

The tracker keeps one in memory record per configured model. No lifecycle state is durable; restart begins every model at `discovered` (OBSERVED Bionic stance: instances rebuild from config).

```
                 first demand (lazy)
 [discovered] ─────────────────────────▶ [loading] ──health ok──▶ [ready]
      ▲                                   │   ▲                    │  ▲
      │ cooldown elapsed                  │   │ retry with         │  │ last call ends
      │                                   │   │ changed plan       │  │
  [failed] ◀── 3rd attempt fails ─────────┘───┘ (≤ 3)       admit  ▼  │
                                                              [executing]
                                                                   │ last call ends
                                                                   ▼
 [unloaded] ◀── drained ── [unloading] ◀── idle TTL / replace ── [idle] ──admit──▶ [executing]
      │                                                                 
      └── next demand ──▶ [loading]

 any of loading, ready, executing, idle ── process exit / connection refused ──▶ [crashed]
 [crashed] ── next demand ──▶ [loading]   (new instance_ref; no restart loop)
```

| Transition | Trigger | Effect on in flight calls |
|---|---|---|
| discovered → loading | first admitted call (lazy) | caller waits up to load deadline |
| loading → ready | health probe passes; managed engine reports ready | none |
| loading → loading | startup failure with a changed plan (port, smaller context) | none; attempt counted |
| loading → failed | third failed attempt, or a plan that changes nothing | waiting callers get `ProviderUnavailable`; cooldown 5 min |
| ready ⇄ executing | slot taken / last slot freed | none |
| executing → idle | zero in flight | idle timer starts from last use |
| idle → unloading | idle TTL (managed engines only), or replacement | admission closes; none in flight |
| executing → unloading | replacement or operator unload | admission closes; in flight calls finish |
| unloading → unloaded | drained | none |
| any → crashed | process exit, connection refused, stream reset mid call | each in flight call fails `ModelCrashed` (retryable) |
| crashed → loading | next demand | new incarnation, new `instance_ref` |
| failed → discovered | cooldown elapsed | none |

**Externally managed servers** (v0.3 scope): GameShark spawns nothing. `loading` is a health probe plus, for LM Studio, the first request (which JIT loads, DOCUMENTED). `unloading` only stops routing. GameShark cannot prevent another app's JIT load from evicting the LM Studio model (DOCUMENTED auto evict); this surfaces as a slow first token or a crash, both handled.

**Model replacement.** A config change for a model drains the old record (`unloading`) and creates a new one at `discovered`. Calls during the gap take the next candidate or queue. Conversations need no migration: the next call resends full context. A smaller replacement window yields `ContextTooLong` at the capability filter, which picks another candidate or refuses; v0.3 never silently truncates context.

## Error taxonomy

| Error | Class | Retry rule | Responsibility effect |
|---|---|---|---|
| `ProviderUnavailable` | retryable | next candidate; same one after state change | none unless attempts exhausted |
| `ModelCrashed` | retryable | next demand rematerializes (new incarnation) | none unless attempts exhausted |
| `Timeout` | retryable | next candidate | none unless attempts exhausted |
| `RateLimited(retry_after)` | retryable | open circuit for `retry_after`; next candidate | none unless attempts exhausted |
| `Overloaded(retry_after)` | retryable, before dispatch | no attempt consumed; caller reschedules | `next_due` set |
| `StreamInterrupted` | retryable | partial text discarded; next candidate | none unless attempts exhausted |
| `ContextConflict` | retryable, before dispatch | reissue with fresh `base_seq`; no attempt consumed | none |
| `Draining` | retryable, before dispatch | caller's work replays under the successor | none |
| `CapabilityUnsupported` | terminal | never | `blocked`, one notification |
| `ContextTooLong` | terminal | never (no silent truncation) | `blocked`, one notification |
| `BudgetExceeded(reset_at)` | terminal for the window | resumes after `reset_at` | `next_due = reset_at`, one notification |
| `EgressDenied` | terminal | after the grant returns | `blocked`, one notification |
| `AuthFailed` | terminal | after operator `resume` | `blocked`, one notification (v0.2 pattern) |
| `InvalidRequest` | terminal | never; a bug | `blocked`, logged |
| `Cancelled` | terminal | never (operator intent, OBSERVED `isExplicitModelUnloadError` lesson) | as requested by the operator |
| `AttemptsExhausted` | terminal | never | `blocked`, one notification |
| `FencedError` (v0.1) | ownership lost | never by this process | successor owns the work |

Attempts per `call_id` are durable and capped at 3 across restarts, so a crash loop cannot multiply spend. Errors raised before dispatch consume no attempt and no budget.

## SQLite persistence

Additive to the v0.2 schema in `prototype/gameshark/store.py`; new tables only, no change to existing ones.

```sql
CREATE TABLE IF NOT EXISTS conversations (
    id                TEXT PRIMARY KEY,
    agent_id          TEXT NOT NULL,          -- owner is the identity, never a model
    responsibility_id TEXT,                   -- owning duty, if any
    privacy           TEXT NOT NULL,          -- local_only | cloud_ok
    next_seq          INTEGER NOT NULL DEFAULT 0,
    created           REAL NOT NULL,
    updated           REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    conversation_id TEXT NOT NULL,
    seq             INTEGER NOT NULL,         -- contiguous from 0
    role            TEXT NOT NULL,            -- system | user | assistant | tool
    content         TEXT NOT NULL,
    call_id         TEXT UNIQUE,              -- set on assistant rows; one per call
    generation      INTEGER NOT NULL,         -- lease generation that wrote it
    created         REAL NOT NULL,
    PRIMARY KEY (conversation_id, seq)
);

CREATE TABLE IF NOT EXISTS inference_calls (
    call_id         TEXT PRIMARY KEY,         -- stable: hash of responsibility or event and step
    agent_id        TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    base_seq        INTEGER NOT NULL,         -- context = messages with seq < base_seq
    request         TEXT NOT NULL,            -- JSON: needs, max_tokens, temperature; no secrets
    status          TEXT NOT NULL,            -- pending | done | failed | cancelled
    attempts        INTEGER NOT NULL DEFAULT 0,
    provider        TEXT,                     -- answering provider and model, set at done
    model           TEXT,
    instance_ref    TEXT,
    degraded        INTEGER NOT NULL DEFAULT 0,
    error_kind      TEXT,
    generation      INTEGER NOT NULL,
    created         REAL NOT NULL,
    updated         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_ledger (
    call_id        TEXT NOT NULL,
    attempt        INTEGER NOT NULL,
    agent_id       TEXT NOT NULL,
    provider       TEXT NOT NULL,
    window_start   REAL NOT NULL,
    reserved_tokens INTEGER NOT NULL,
    reserved_micros INTEGER NOT NULL,
    tokens_in      INTEGER,
    tokens_out     INTEGER,
    cost_micros    INTEGER,
    status         TEXT NOT NULL,             -- reserved | settled | lost
    PRIMARY KEY (call_id, attempt)
);

CREATE TABLE IF NOT EXISTS agent_budgets (
    agent_id        TEXT NOT NULL,
    window_s        REAL NOT NULL,
    max_tokens      INTEGER NOT NULL,
    max_cost_micros INTEGER NOT NULL,
    PRIMARY KEY (agent_id, window_s)
);
```

Requirements:

- Every write runs inside `BEGIN IMMEDIATE` with `LeaseManager.guard_in_txn` (the `AgentSupervisor._fenced` pattern). WAL and `synchronous=FULL` stay.
- Two durable writes per attempt, the same irreducible shape as the effect journal: **reserve** (insert or bump `inference_calls.attempts`, insert ledger row `reserved`) before dispatch; **commit** (insert assistant message at `seq = base_seq`, bump `next_seq`, mark call `done` with provider and model, settle ledger) in one transaction after.
- Commit fails closed on any of: stale generation (`FencedError`), `call_id` already done (return the committed row), `next_seq != base_seq` (`ContextConflict`).
- Recovery marks every `reserved` ledger row from an older generation `lost` at full reservation.
- Model instances, queues, slots, and circuits are never persisted.
- Content is plaintext in v0.3, like the v0.2 notification feed; encryption at rest is deferred.

## Ownership and concurrency invariants

These extend I1 to I7 in `continuity-invariants.md`.

- **R1 Identity independence.** No identity, persona, conversation, or responsibility field is ever read from a provider. Switching provider or model leaves the agent uuid and every durable row unchanged.
- **R2 Single committed result.** At most one assistant message per `call_id` (`UNIQUE` constraint) and at most one transition to `done`.
- **R3 Linear context.** An assistant message commits only at `seq = base_seq = next_seq`; message sequences stay contiguous.
- **R4 Fenced writes.** Every inference write carries the current lease generation; a stale process commits nothing.
- **R5 No effect from uncommitted output.** An `EffectJournal` intent derived from model output must reference a `done` `call_id`. Streamed chunks never reach an external surface.
- **R6 Reserve before spend.** A ledger reservation commits before any request leaves the process, and admitted spend never exceeds the window budget.
- **R7 Egress under grant.** No request byte reaches a cloud provider unless the conversation is `cloud_ok` and `inference.cloud` is effective at dispatch time.
- **R8 Bounded work.** Attempts per call ≤ 3 across restarts; queue depth per provider ≤ its bound; in flight per provider ≤ its slots; in flight per conversation ≤ 1.
- **R9 Operator intent is terminal.** Cancel, unwatch, and grant revocation are never retried or resurrected by recovery.

**Explicitly not guaranteed.** Exactly once provider execution, exactly once billing, deterministic regeneration after a crash (a rerun may produce different text; nothing observed the first text, so nothing depends on it), and server side abort on cancel.

## Recovery scenarios

| Scenario | What happens | Guarantee kept | Test |
|---|---|---|---|
| Local provider crashes mid stream | `ModelCrashed`; partial text dropped; ledger settles `lost`; next candidate or rebuilt instance answers | R2, R6, R8 | `test_crash_mid_stream_fails_over` |
| Cloud returns 429 | circuit opens for `retry_after`; local candidate answers if permitted, else caller reschedules | R7, R8 | `test_rate_limit_opens_circuit` |
| SIGKILL after reserve, before dispatch (E2) | restart marks reservation `lost`; event or responsibility replays; same `call_id` takes attempt 2 | R2, R6 | `test_kill_e2_reruns_once` |
| SIGKILL after provider returned, before commit (E3) | answer lost; replay reruns attempt 2; one message commits | R2, R5 | `test_kill_e3_one_message` |
| SIGKILL after commit, before handler continues (E4) | replay finds `done`, returns the committed text with no provider call | R2 | `test_kill_e4_short_circuit` |
| Crash loop on one call | third attempt fails → `AttemptsExhausted`, responsibility `blocked`, one notification | R8 | `test_attempt_cap_survives_restarts` |
| Budget exhausted mid responsibility | `BudgetExceeded`; `next_due = reset_at`; zero provider calls until reset | R6 | `test_budget_refusal_no_dispatch` |
| Local model replaced by config | old record drains; new loads lazily; conversation continues with full context resent | R1, R3 | `test_model_swap_keeps_identity` |
| Stale worker commits after takeover | `FencedError`; no message row; successor's answer is the only one | R2, R4 | `test_stale_commit_fenced` |
| `inference.cloud` revoked with a pending cloud bound call | replay filters cloud out; local answers or `EgressDenied` blocks; zero cloud bytes | R7, R9 | `test_revoke_egress_pending_call` |
| Two calls race on one conversation | second waits; if forced, commit sees `next_seq` moved and raises `ContextConflict` | R3 | `test_conversation_linear` |

## Graceful handoff

Builds on the `draining` lease state proposed in `bionic-to-gameshark.md` (change 1), which v0.3 must implement first.

1. **Freeze.** On SIGTERM or operator stop, the router enters `draining`; admission returns `Draining`. Callers' work stays durable: inbox events remain pending; responsibilities keep their `next_due`.
2. **Drain.** Wait for in flight calls up to `drain_deadline` (default 10 s, below the lease TTL). The tick thread commits each completed result under the still valid generation.
3. **Checkpoint.** Cancel the remainder. Their calls stay `pending` with `attempts` already counted; their ledger rows settle `lost`. No partial output commits.
4. **Release.** `LeaseManager.release` expires the lease and keeps the generation.
5. **Fence.** The successor acquires generation + 1. Any late commit from an old executor thread or zombie process fails `guard_in_txn`.
6. **Resume.** The successor runs v0.1 recovery (reconcile effects, replay inbox), marks old reservations `lost`, and lets owners reissue. Recovery never drives an `inference_calls` row directly; its owner (event handler or responsibility step) does, with the same `call_id`. A pending call with no live owner is impossible because inbox events survive until acked (I5) and responsibilities stay outstanding until `done`.

SIGKILL skips steps 1 to 4; steps 5 and 6 still hold, at the cost of the lost reservations and one rerun per in flight call.

## Minimal v0.3 scope

Ordered increments; each lands with its own tests and leaves the suite green.

0. **Prerequisites.** Done. PR #6 fixed the `LeaseManager.renew` double rollback that could mask `FencedError`. Graceful draining added the `draining` lease state, a heartbeat that renews through the drain, bounded drain deadlines, and release fencing; see `prototype/README.md`, Graceful drain and handoff.
1. **Contracts.** Done. `inference/types.py` (immutable values, error taxonomy), `inference/provider.py` (`Provider`, request scoped `ProviderCall`), `inference/fake.py` (`FakeProvider` with deterministic text, scripted delays, faults per chunk index, invocation counts). Reuses the existing `CancelToken`. Pure unit tests in `tests/test_inference_contracts.py`.
2. **Journal.** Tables plus `InferenceJournal` (reserve, commit, short circuit, recovery settlement) with crash points E1 to E4 using the existing `crash_at` hook.
3. **Router.** Capability, policy, ordering, health, admission, bounded queues, failover, degraded flag. Fake providers only.
4. **OpenAI compatible provider.** stdlib HTTP plus SSE; tested against a local HTTP fixture in the style of `tests/test_github_rest.py`. No new runtime dependency.
5. **Lifecycle tracker.** Health probes, crashed, failed with cooldown, lazy load, replacement drain. Externally managed servers only.
6. **Worker integration.** Executor, completion queue, tick thread commits, handoff steps 1 to 6; subprocess SIGKILL tests in the style of `tests/test_daemon.py`.
7. **Live smoke (opt in, not CI).** One local server (SGLang or `mlx_lm.server`) and one cloud endpoint, gated by environment variables; records first token latency, cancel behavior, and slot behavior per provider.

## Deferred

- Spawning and owning engine processes (SGLang, MLX), load guardrails on memory, idle TTL unload of managed engines
- KV cache or prefix aware routing, speculative decoding, batching awareness
- Native cloud SDKs and the Anthropic Messages API (LM Studio documents an Anthropic compatible surface; revisit later)
- Tool call normalization across providers; v0.3 passes OpenAI format through
- Streaming partial output to users or external surfaces
- Long term memory, summarization, retrieval, context truncation policies
- Embedding models
- Multi host routing and an LM Link style device mesh
- Cost or quality optimizing routing, learned preferences
- Encryption of conversation content at rest

## Acceptance tests and success criteria

All run in CI on `FakeProvider` and a local HTTP fixture; no network, no model weights.

| Test | Pass criterion |
|---|---|
| Kill at each crash point E1 to E4, 50 seeded runs each | exactly one assistant message per `call_id`; agent uuid identical before and after; zero orphan `reserved` ledger rows after recovery |
| Randomized fault soak: 1,000 calls, faults at random chunk indices across 3 fake providers | every call ends `done` or a terminal error; zero leaked slots; queue depth never exceeds its bound; attempts never exceed 3 |
| Egress denial | with `local_only` or the grant revoked, the cloud fake records zero requests |
| Budget | admitted spend never exceeds the window limit; refusal happens with zero provider calls |
| Provider swap mid conversation | uuid, conversation rows, and responsibility state unchanged; the next call resends the full context |
| Stale commit | a commit at generation N after takeover at N + 1 raises `FencedError` and writes nothing |
| Graceful drain | calls shorter than the deadline commit; longer ones end `pending`; drain completes within `drain_deadline` + 2 s |
| Startup retry | `loading` makes at most 3 attempts; each attempt's plan differs; a plan that changes nothing stops at once |
| Replay short circuit | replay of a `done` call makes zero provider calls |
| Router overhead | selection plus admission p99 under 1 ms excluding SQLite; full durable overhead (two fenced writes) p99 under 10 ms on a local SSD, measured on 1,000 fake calls |
| Regression | all v0.1 and v0.2 tests pass unchanged; Ruff clean |

## Opportunity cost

- Two fsynced writes per attempt cost milliseconds against inference latency of hundreds of milliseconds or more. Skipping them would save under 1 percent of latency and lose replay short circuit, budget accounting, and fencing.
- Local first saves money and keeps data on the host, at the cost of answer quality for hard prompts. The preference list overrides it per agent.
- One OpenAI compatible transport covers four targets for the cost of one client, at the price of lowest common denominator features (no native prompt caching controls, thinner tool semantics). Native clients wait until a measured need appears.
- Refusing instead of queueing without bound makes overload loud and keeps the tick thread live, at the cost of rescheduled work under bursts.
