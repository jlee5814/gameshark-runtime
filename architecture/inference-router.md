# GameShark inference router: v0.3 design

Status: design, with steps 0 to 7 of Minimal v0.3 scope implemented (graceful drain; provider contracts, the inference journal, the router, the OpenAI compatible HTTP provider, the model lifecycle tracker, the executor with daemon integration in `prototype/gameshark/inference/` and `prototype/gameshark/worker.py`, and the opt in live harness in `prototype/live/`). Step 7 ran live against local SGLang only; the cloud path is CI tested but not run live. v0.1 and v0.2 runtime code stays unchanged.

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
| A routed call through `OpenAICompatProvider` to an externally managed SGLang/MLX server serving `mlx-community/Qwen3-4B-4bit` committed durably to SQLite, and replay after constructing a new supervisor made zero provider calls | OBSERVED (one manual live run after PR #15, not CI) | Supports the step 4 transport and the replay short circuit on a real server; not a CI guarantee. The step 5 tracker has no live run yet |
| Whether a model is resident in GPU memory | UNKNOWN through the OpenAI API (`/v1/models` lists names, not residency) | Residency is inferred only from a streamed token on the current incarnation |

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

**Revised in step 6.** The heartbeat thread (graceful drain, step 0) already renews independently, so the remaining cost of a blocking call was responsiveness: no controls, no polls, no other calls. The router splits each attempt into phases; executor threads run only `load` (tracker admit, probes, replacement wait) and `execute` (stream and verify), which touch no SQLite. Every journal phase, the continuation, and the ack run on the tick thread. Async routing requires the tracker, so health probes never run on the tick thread. Slots are claimed without queueing and calls in flight are capped at the completion queue depth, so executors never wait on each other and a completion never blocks. An event that schedules a call stays pending, claimed in memory, until its continuation ran.

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

v0.3 ships two providers: `FakeProvider` (deterministic text, scripted faults at each chunk index, scripted latency) and `OpenAICompatProvider` (stdlib `http.client`, SSE parsing, `/v1/chat/completions` and `/v1/models`, base URL plus optional key). Revised in step 4: locality derives from the validated base URL, never from a label. Only a loopback IP literal is local (`localhost` is refused, since its resolution is not verifiable at connect time); every other host is cloud and must use HTTPS. A loopback URL never needs the egress grant; a cloud URL always does.

## Router

### Selection

Input: `needs`, conversation privacy, estimated prompt tokens, `max_tokens`. Output: an ordered candidate list, or a typed refusal.

1. **Capability filter.** Keep models whose capabilities cover `needs` and whose `context_window >= prompt_tokens + max_tokens`. Empty set: `CapabilityUnsupported` or `ContextTooLong`.
2. **Policy filter.** Drop cloud candidates when privacy is `LOCAL_ONLY` or the durable grant `inference.cloud` is not effective. If only cloud candidates remained: `EgressDenied`.
3. **Ordering.** The agent's configured preference list if present; else local first, cheapest cloud second.
4. **Health filter.** Drop models in `failed` cooldown or with an open circuit after `RateLimited`. Keep `crashed` and `unloaded`; they rematerialize lazily.
5. **Admission.** Take a slot or a queue position on the first candidate that has one, then reserve budget. Revised in step 3: slot first, so no reservation sits in a queue, where a crash would charge it as lost without any dispatch.

On a retryable error the router moves to the next candidate. A move counts as an attempt and is a changed plan; the same candidate is retried only after its state changed (crashed instance rebuilt, `retry_after` elapsed). The committed call records `degraded = 1` when the answering model differs from the first candidate, so audit can tell a fallback answer from the requested one (OBSERVED lesson from `recoveryOriginalValues`).

Policy refusals (`EgressDenied`, `BudgetExceeded`, `BudgetUnenforceable`) end the walk at once and never advance to the next candidate, so a policy denial can never turn into cloud fallback. Only a retryable failure after dispatch, or a capability refusal from `generate`, moves on.

### Budgets and admission

`agent_budgets` sets, per agent and window, `max_tokens` and `max_cost_micros`. Admission writes a `budget_ledger` row with status `reserved` before dispatch:

- reserved tokens = prompt bound + `max_tokens`
- reserved cost = input price × prompt bound + output price × `max_tokens` (zero for local)

Windows are fixed and aligned: a window of `w` seconds starts at `floor(now / w) * w`, and `BudgetExceeded.reset_at` is the next boundary (infinity when one request alone exceeds a limit). Spend counts settled attempts at actual usage and lost or still reserved attempts at their full reservation. The check and the ledger insert run in the reserve transaction (`BEGIN IMMEDIATE`), so concurrent admissions on any connection or process serialize and cannot both take the last of a budget; a refusal rolls back and consumes no attempt. On completion the row settles to actual usage; on crash or cancel it settles to the full reservation (`lost`). Spend counts attempts, not commits: cloud providers bill a discarded answer. Fixed windows admit up to twice a limit across a boundary.

**Token accounting limitation.** The first draft claimed the budget never overruns because output cannot exceed `max_tokens`. That holds only if the prompt term bounds the provider's real prompt count, and `estimate_tokens` (whitespace words) bounds no real tokenizer: subword splits, punctuation, non Latin text, and chat templates all add tokens. Step 3 makes the prompt bound an explicit per provider policy (`TokenBound`):

| Bound | Reservation | Budget |
|---|---|---|
| `TRUSTED_BYTES` | UTF 8 bytes + 8 per message + 16 | hard, valid only for byte level BPE or byte fallback tokenizers (at least one byte per token), which the operator asserts in configuration; roughly 3 to 4 times the true count for English |
| `ESTIMATE` | whitespace words | soft. A paid provider under any configured budget is refused before dispatch (`BudgetUnenforceable`) unless the durable grant `inference.cloud.soft_budget` is effective. Local providers cost nothing and run soft without a grant |

Actual usage always settles unmodified and is never truncated to fit the reservation. An attempt whose actual tokens or cost exceed its reservation returns an `Overrun` and appears in `InferenceJournal.overruns()`; later admissions see the actual spend, so a soft overrun shrinks what remains of the window. A soft budget can overrun by the estimation error of the attempts in flight when it runs out. A trusted provider that overruns violated its declared bound: the overrun is flagged hard and the provider is demoted to `ESTIMATE` for the process. No cloud budget is hard until a provider reports a trusted bound. Revised in step 4: a provider that reports no usage returns `usage=None`, never an estimate; the attempt commits and its ledger row settles `unmetered` at the full reservation, which bounds actual spend only under a trusted prompt bound.

### Concurrency, queueing, backpressure

| Limit | Default | When full |
|---|---|---|
| Slots per provider | `max_concurrency` (SGLang 8, MLX 1, LM Studio 1, cloud 4; config) | enqueue |
| Queue depth per provider | 16 | `Overloaded(retry_after)` |
| In flight per agent | 4 | `Overloaded(retry_after)` |
| In flight per conversation | 1 | `Overloaded(retry_after)`; the caller reissues after the prior call's commit |
| Executor threads | sum of slots | never exceeded; slots bound it |

Queues are FIFO per provider; a freed slot passes directly to the queue head, so no waiter spins or races a newcomer. Queue waits are bounded (`max_queue_wait_s`, default 0: refuse at once) and wake on cancellation. A full queue fails fast rather than blocking the tick thread. The caller does not spin: an inbox event stays pending and replays; a responsibility sets `next_due = now + retry_after`, reusing the v0.2 scheduler. `retry_after` estimates queue depth × median call latency, capped at 60 s.

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

**Revised in step 5** (`inference/tracker.py`, opt in through `InferenceRouter(tracker=...)` until step 6 wires the worker):

- *Ready versus idle.* `ready` means a probe passed and no token has streamed on this incarnation; `idle` means it served one. The last call to end moves `executing` to `idle`, or back to `ready` when no token arrived. This resolves the diagram's two exits from `executing`.
- *Three readiness levels.* Endpoint ready (`/v1/models` answers), model available (the id is listed), GPU resident (a token streamed on this incarnation). The OpenAI API exposes no residency query, so residency is never assumed from a probe.
- *Load plans.* `probe` (1 s, listed), `patient` (2 s delay, 10 s, listed), `endpoint_only` (2 s delay, 10 s, unlisted accepted for LM Studio JIT). A repeated plan or a fourth plan is refused at configuration, which realizes "a plan that changes nothing stops at once".
- *Suspicion before crash.* Connection refused or a reset mid stream is one request's observation, not proof the process died. It records a suspicion; the next demand probes, and only an endpoint that no longer answers becomes `crashed`. Request level errors never change state. A verify TTL (30 s) also reprobes unused models on demand, which detects LM Studio auto evict as `unloaded`.
- *Bounded recovery.* Three consecutive crash cycles without a completed call move the model to `failed` with the same 5 minute cooldown.
- *Replacement drain.* The drain is bounded; calls still running at the deadline are cancelled through a child token, settled `lost`, and rerun on the successor rather than treated as operator cancellation. A finished call seals its use under the tracker lock before it commits, and eviction skips sealed calls, so a provider that ignores the cancel and answers late never commits for the retired configuration. Successor settings are validated before the old record changes. A caller that selected the old configuration waits up to `replace_wait_s`, then the router selects again (at most twice), so capability, privacy, and the egress grant are judged against the new configuration. Tracker refusals (`ModelUnavailable`, `ModelReplaced`) are before dispatch: no attempt, no budget.
- *Reuse.* `Scheduler` stays the sole slot and queue owner; `ProviderHealth` keeps circuits and auth blocks (`blocked`) and gains `reset` for a replaced name; cancellation is the existing `CancelToken`.

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
| `BudgetUnenforceable` | terminal, before dispatch | after a trusted bound or the soft budget grant | `blocked`, one notification |
| `AuthFailed` | terminal, no failover | replay refused until the provider's config fingerprint changes (recorded in `error_kind`) | `blocked`, one notification (v0.2 pattern) |
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
    status         TEXT NOT NULL,             -- reserved | settled | unmetered | lost
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
- Recovery marks every `reserved` ledger row that no live attempt holds `lost` at full reservation. Revised in step 2: a same owner restart on a live lease keeps its generation (`LeaseManager.acquire`), so generation alone cannot tell an abandoned reservation from a live one. Recovery runs at start, before serving, when no call is in flight; commit also requires `attempts` to equal its attempt and its ledger row to be `reserved`, which fences a same generation zombie.
- Model instances, queues, slots, and circuits are never persisted.
- Content is plaintext in v0.3, like the v0.2 notification feed; encryption at rest is deferred.

## Ownership and concurrency invariants

These extend I1 to I7 in `continuity-invariants.md`.

- **R1 Identity independence.** No identity, persona, conversation, or responsibility field is ever read from a provider. Switching provider or model leaves the agent uuid and every durable row unchanged.
- **R2 Single committed result.** At most one assistant message per `call_id` (`UNIQUE` constraint) and at most one transition to `done`.
- **R3 Linear context.** An assistant message commits only at `seq = base_seq = next_seq`; message sequences stay contiguous.
- **R4 Fenced writes.** Every inference write carries the current lease generation; a stale, released, or expired owner commits nothing. `guard_in_txn` checks expiry against the injected clock inside the write transaction, so a late completion after expiry fails closed even with no successor (issue #10).
- **R5 No effect from uncommitted output.** An `EffectJournal` intent derived from model output must reference a `done` `call_id`. Streamed chunks never reach an external surface.
- **R6 Reserve before spend.** A ledger reservation commits before any request leaves the process, and reserved spend never exceeds the window budget. Actual spend stays within it only under a trusted token bound; see the token accounting limitation.
- **R7 Egress under grant.** No request byte reaches a cloud provider unless the conversation is `cloud_ok` and `inference.cloud` is effective at dispatch time.
- **R8 Bounded work.** Attempts per call ≤ 3 across restarts; queue depth per provider ≤ its bound; in flight per provider ≤ its slots; in flight per conversation ≤ 1.
- **R9 Operator intent is terminal.** Cancel, unwatch, and grant revocation are never retried or resurrected by recovery.

**Explicitly not guaranteed.** Exactly once provider execution, exactly once billing, deterministic regeneration after a crash (a rerun may produce different text; nothing observed the first text, so nothing depends on it), and server side abort on cancel.

## Recovery scenarios

| Scenario | What happens | Guarantee kept | Test |
|---|---|---|---|
| Local provider crashes mid stream | `ModelCrashed`; partial text dropped; ledger settles `lost`; next candidate or rebuilt instance answers | R2, R6, R8 | `test_crash_mid_stream_fails_over` |
| Cloud returns 429 | circuit opens for `retry_after`; local candidate answers if permitted, else caller reschedules | R7, R8 | `test_rate_limit_opens_circuit` |
| SIGKILL before reserve (E1) | nothing durable for the call; replay runs attempt 1 | R2 | `test_sigkill_at_crash_point_recovers_to_one_answer[E1_before_reserve]` |
| SIGKILL after reserve, before dispatch (E2) | restart marks reservation `lost`; event or responsibility replays; same `call_id` takes attempt 2 | R2, R6 | `test_sigkill_at_crash_point_recovers_to_one_answer[E2_after_reserve]` |
| SIGKILL after provider returned, before commit (E3) | answer lost; replay reruns attempt 2; one message commits | R2, R5 | `test_sigkill_at_crash_point_recovers_to_one_answer[E3_after_provider]` |
| SIGKILL after commit, before handler continues (E4) | replay finds `done`, returns the committed text with no provider call | R2 | `test_sigkill_at_crash_point_recovers_to_one_answer[E4_after_commit]` |
| Crash loop on one call | third attempt fails → `AttemptsExhausted`, responsibility `blocked`, one notification | R8 | `test_attempt_cap_survives_crash_loop_restarts` |
| Budget exhausted mid responsibility | `BudgetExceeded`; `next_due = reset_at`; zero provider calls until reset | R6 | `test_budget_refusal_no_dispatch` |
| Local model replaced by config | old record drains; new loads lazily; conversation continues with full context resent | R1, R3 | `test_replacement_keeps_identity_and_conversation_and_resends_full_context`, `test_replacement_drains_the_active_call_and_serves_new_arrivals`, `test_evicted_call_returning_late_never_commits_and_reruns_on_the_successor`, `test_completion_and_eviction_race_linearizes`, `test_invalid_successor_leaves_the_active_model_usable` |
| Stale worker commits after takeover | `FencedError`; no message row; successor's answer is the only one | R2, R4 | `test_stale_owner_cannot_commit_after_takeover` |
| `inference.cloud` revoked with a pending cloud bound call | replay filters cloud out; local answers or `EgressDenied` blocks; zero cloud bytes | R7, R9 | `test_revoke_egress_pending_call` |
| SIGKILL mid HTTP stream in the daemon | reservation settles `lost` at restart; the replayed event reissues the same `call_id` as attempt 2; one message | R2, R6 | `test_sigkill_during_active_http_inference_recovers_without_duplicates` |
| Stale owner resumes after takeover with an answer in hand | commit fails the fence; no message, ack, or effect; the successor's answer is the only one | R2, R4, R5 | `test_stale_owner_completion_after_takeover_writes_nothing` |
| Two calls race on one conversation | second waits; if forced, commit sees `next_seq` moved and raises `ContextConflict` | R3 | `test_two_calls_on_one_conversation_conflict_then_rebase` |

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
2. **Journal.** Done. Tables plus `InferenceJournal` (reserve, commit, short circuit, recovery settlement) with crash points E1 to E4 using the existing `crash_at` hook; `collect` verifies chunk and result identity; new errors `ResultMismatch`, `StaleAttempt`, `OwnershipMismatch`, `CallCancelled`; effects citing an uncommitted `call_id` are refused (R5). Tests in `tests/test_inference_journal.py`; see `prototype/README.md`, Inference journal.
3. **Router.** Done. `inference/router.py` (selection, policy, failover, degraded flag, `TokenBound`), `inference/scheduler.py` (slots, bounded FIFO queues, per agent and per conversation limits), `inference/budget.py` (fixed window admission inside the reserve transaction). Journal additions are optional arguments (`prompt_tokens`, `admit`, `error_kind`, `degraded`); E1 to E4 unchanged. Fake providers only; no worker executor (step 6) or lifecycle tracker (step 5): health is a cached `health()` probe plus rate limit circuits and auth blocks. Tests in `tests/test_inference_router.py`; see `prototype/README.md`, Inference router.
4. **OpenAI compatible provider.** Done. `inference/openai_compat.py`: stdlib `http.client`, incremental SSE decoding, endpoint validated locality, cancellation and deadlines inside socket reads, typed error mapping, key redaction, `unmetered` settlement for missing usage. Tested against a loopback fixture in `tests/test_openai_compat.py`, including E1 to E4 with the HTTP provider; see `prototype/README.md`, OpenAI compatible provider. No new runtime dependency.
5. **Lifecycle tracker.** Done. `inference/tracker.py`: nine states with an enforced transition table, single flight lazy load with three distinct plans, suspicion before crash, bounded recovery, cooldown, verify TTL, and replacement drain with reselection. Externally managed servers only; memory only. Opt in on the router; required on the async path from step 6. Tests in `tests/test_model_tracker.py`; see `prototype/README.md`, Model lifecycle tracker.
6. **Worker integration.** Done, in two PRs. Part 1: router phases, `inference/executor.py` (fixed pool, bounded completion queue, coordinator pump), deferred event ownership in `EventRouter` and the supervisor, `inference_calls.finish_reason`, and the async demonstration adapter, tested in process (`tests/test_inference_executor.py`). Part 2: `run_forever` pumps completions between controls and polls, wakes on each completion, drains inference under SIGTERM (commit what finishes, cancel the rest, drop a deaf provider before the grace abort), and drops calls without writing on ownership loss; real subprocess tests for E1 to E4, SIGKILL mid HTTP stream, SIGTERM, takeover, and polling during a blocked call (`tests/test_daemon_inference.py`). See `prototype/README.md`, Inference executor and daemon integration.
7. **Live smoke (opt in, not CI).** One local server (SGLang or `mlx_lm.server`) and one cloud endpoint, gated by environment variables; records first token latency, cancel behavior, and slot behavior per provider. Local done; cloud not run. `prototype/live/smoke.py` drives real daemon subprocesses on temporary databases through an observing provider wrapper and writes `results.json` and `summary.md`; deterministic harness tests in `tests/test_live_harness.py`. Run 1 against SGLang serving Qwen3 4B passed every runtime check: TTFT p50 0.065 s warm, cancel 0.3 ms after the token, peak in flight equal to 4 slots, the attempt cap held across three SIGKILLs, and fencing held. Qwen3 emits think markers under `/no_think`, so answers commit and stay blocked as designed. OBSERVED server defect: after aborted streams, the SGLang prefix cache returned another request's output for an identical prompt. Cloud acceptance needs credentials and explicit opt in; see `prototype/README.md`, Live inference acceptance.

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
| Graceful drain | calls shorter than the deadline commit; longer ones end `pending`; drain completes within `drain_deadline` + 2 s (`test_sigterm_drains_a_short_call_and_cancels_a_long_one`, `test_sigterm_with_a_provider_ignoring_cancel_ends_before_the_grace_abort`) |
| Startup retry | `loading` makes at most 3 attempts; each attempt's plan differs; a plan that changes nothing stops at once (`test_unavailable_http_endpoint_fails_after_three_plans_with_zero_requests`, `test_transition_table_plans_and_wiring_are_validated`) |
| Replay short circuit | replay of a `done` call makes zero provider calls |
| Router overhead | selection plus admission p99 under 1 ms excluding SQLite; full durable overhead (two fenced writes) p99 under 10 ms on a local SSD, measured on 1,000 fake calls |
| Regression | all v0.1 and v0.2 tests pass unchanged; Ruff clean |

## Opportunity cost

- Two fsynced writes per attempt cost milliseconds against inference latency of hundreds of milliseconds or more. Skipping them would save under 1 percent of latency and lose replay short circuit, budget accounting, and fencing.
- Local first saves money and keeps data on the host, at the cost of answer quality for hard prompts. The preference list overrides it per agent.
- One OpenAI compatible transport covers four targets for the cost of one client, at the price of lowest common denominator features (no native prompt caching controls, thinner tool semantics). Native clients wait until a measured need appears.
- Refusing instead of queueing without bound makes overload loud and keeps the tick thread live, at the cost of rescheduled work under bursts.
