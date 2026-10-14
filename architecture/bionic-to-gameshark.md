# Bionic (LM Studio) to GameShark translation

How each observed Bionic mechanism maps to a GameShark persistent agent requirement. Confidence tags carry over from `research/bionic/deep-analysis.md`. Observed means read statically from shipped artifacts; no Bionic behavior here is runtime verified. GameShark code is clean room; Bionic informs the problem shape only.

## Translation table

| Bionic mechanism (evidence, confidence) | Problem it solves | Generalizable? | GameShark equivalent | Extra guarantee GameShark needs |
|---|---|---|---|---|
| Held SQLite write transaction as lease (`LocalInferenceHandoff.acquire`, `BEGIN IMMEDIATE`, Observed) | One writer among cooperating processes; instant release on death | Yes, per host | `InstanceLock` (`worker.py`, `flock`) | Keep the TTL lease plus fencing generation (`lease.py`) for cross host ownership; the held lock only short circuits same host restart |
| Drain before release (`flushing` phase, `canWrite` false while lease held, Observed) | Hand off ownership without torn shared writes | Yes, strongly | Missing. `LeaseManager.release` expires the row directly | Add a draining state: stop admitting effects, finish or abandon journaled in flight effects, then release. A successor must never observe a half applied effect |
| Reader protection of shared packs (`.packs` read transaction, `SharedPacksInUseError`, Observed) | Prevent deletion of shared assets another process still uses | Partially | Shared adapter artifacts across agents | Deletion of shared state must fail fast while any reader holds it, never block silently |
| Crash means unload, never restart (`unloadReason type crashed`, Observed) | Avoid restart storms; keep instance state honest | Yes | Supervisor watchdog over adapters (`supervisor.py`) | A worker crash marks the worker gone; the responsibility stays `pending` and is never dropped or duplicated |
| Rebuild at the demand edge under a per key mutex (`getOrLoadInstanceReference`, `ngManagedLoadMutex`, `NGRetryableError`, Observed) | Recover lazily, once, with fresh config | Yes, strongly | `run_forever` schedules due responsibilities (`worker.py`) | Rematerialize the adapter worker when its next responsibility comes due; serialize per responsibility so two triggers never build two workers |
| Explicit unload excluded from retry (`isExplicitModelUnloadError`, Observed) | Distinguish operator intent from failure | Yes | `revoke`, `unwatch` controls | An operator revoke must be terminal for retries; recovery may never resurrect a revoked responsibility |
| Admission before spawn (`ModelLoadGuardrailPlanner`, typed `ModelLoadingGuardrailRejectionError`, Observed) | Refuse work the machine cannot hold, before side effects | Yes | Responsibility acceptance (`ResponsibilityStore.upsert`) | Budget check at acceptance with a typed refusal, journaled, so the agent never silently holds a responsibility it cannot serve |
| Bounded, plan driven startup retry (`startRuntime`, 3 attempts, `getStartupRetryPlan`, `recoveryOriginalValues`, Observed) | Retry only when something changed; record drift | Yes | Adapter retry and backoff (GitHub monitor) | Each retry names its change; the original intent stays journaled beside the degraded parameters |
| Idle TTL from last use (`lastUsedTimeSignal` timer, `jitModelTTL`, Observed) | Free resident capacity without losing anything durable | Yes | Not present | Evict idle adapter workers by last use; never evict responsibilities. TTL applies to residency only |
| Opaque instance reference vs human identifier (`instanceReference`, `identifier` with `bump`, Observed) | Stable unique handle separate from display name | Yes | Agent id vs owner token (`identity.py`, `lease.py`) | Already present in GameShark; Bionic confirms the split. Instance handles are minted per incarnation and never reused |
| Durable config, ephemeral instances (`modelInstancesSignal` in memory only, Observed) | Restart is cheap because instances rebuild from config | Yes, strongly | Process as cattle (`continuity-invariants.md`) | Already the GameShark stance; Bionic is a second production system confirming it |

## Persistence taxonomy, revisited

The five kinds from `discord-to-gameshark.md`, with Bionic's position on each.

### Process persistence
Bionic provides none for workers and does not depend on it for the orchestrator. Model processes are disposable and rebuilt on demand. **Confirms GameShark's rule: never depend on process persistence.**

### Session continuity
Bionic's session engine survives a model unload mid prediction by raising a retryable error and reloading. The session survives; the worker does not. **GameShark adapters should treat a worker loss inside a session as retryable, attached to the same session and identity.**

### Identity persistence
Bionic's instance references are per incarnation and deliberately not persistent; durable identity lives in model keys and config. **GameShark keeps its explicit durable agent identity; Bionic shows the per incarnation handle should be separate and disposable.**

### Task persistence
Bionic has little: an in flight prediction dies with its worker, and the retry restarts it from the prompt. Acceptable for idempotent inference, insufficient for consequential effects. **GameShark keeps step checkpoints; Bionic's retry model fits only reads and idempotent work.**

### Durable responsibility ownership
Bionic's closest primitive is the local inference lease: an exclusive, crash released, drain before release right to write shared state. It covers ownership of a resource, not of a standing duty. **GameShark's target remains strictly larger, and the Bionic lease contributes the drain protocol and the held lock fast path.**

## Proposed changes, not yet made

Ranked by value per effort. None is implemented; this study changes no runtime code.

1. **Draining lease state.** Add `draining` between `held` and `released`: the holder stops admitting new effects, reconciles pending journal entries, then releases. Cost: one state, one transition test. Opportunity cost: low; it closes a real gap in handoff.
2. **Crash as state, rematerialize on due.** Make adapter worker death mark the worker absent instead of triggering an immediate restart; the scheduler rebuilds it when its next responsibility is due, under a per responsibility mutex. Cost: moderate refactor of `supervisor.py` and `worker.py`. Opportunity cost: medium; it trades fast recovery for restart storm immunity.
3. **Typed admission at acceptance.** Gate `upsert` on a capacity budget with a journaled refusal. Cost: small. Opportunity cost: low until GameShark runs many responsibilities per agent.
4. **Retry plans that must change something.** Require each adapter retry to carry a changed parameter or stop. Cost: small. Opportunity cost: low; it also improves logs.
5. **Idle eviction of resident workers.** Only matters once adapters hold heavy resources. Defer.

## Inference router mapping (v0.3 design)

The router design in `inference-router.md` reuses the same mechanisms for model inference. Tags follow the deep analysis; Documented rows come from LM Studio public documentation fetched 2026-10-14.

| Bionic mechanism (confidence) | Router component | Change GameShark makes |
|---|---|---|
| In memory instances rebuilt from config (`modelInstancesSignal`, Observed) | `ModelTracker`, memory only | Persist conversations, calls, and budgets; never instances |
| Lazy load under a per model mutex (`getOrLoadInstanceReference`, Observed) | `discovered → loading` on first admitted call | Per conversation limit of one in flight call, plus a commit check on `next_seq` |
| Crash marks the instance and resolves its unload (`unloadReason type crashed`, Observed) | `crashed` state, `ModelCrashed` retryable error | Attempts per call capped at 3 across restarts, so a crash loop cannot multiply cloud spend |
| Retryable unload versus explicit unload (`NGRetryableError`, `isExplicitModelUnloadError`, Observed) | Retryable versus terminal taxonomy; `Cancelled` terminal | Operator intent (cancel, revoke) is never retried (R9) |
| Guardrail plan before spawn, typed rejection (`ModelLoadGuardrailPlanner`, Observed) | Admission: budget reservation before dispatch | Reservation bounds spend by `max_tokens`, so budgets never overrun (R6) |
| Startup retries bounded at 3 with changed plans (`getStartupRetryPlan`, Observed) | `loading → loading` retries; failover counts as a changed plan | Record `degraded` when the answering model differs from the first choice |
| Parallel admission per instance (`ParallelPredictionQueue`, Observed) | Slots per provider plus a bounded FIFO queue | Full queue fails fast with `Overloaded(retry_after)` instead of growing |
| Drain before release (`LocalInferenceHandoff` `flushing`, Observed) | Handoff: freeze, drain, release, fence, resume | Fence generation rejects late commits from a predecessor |
| JIT idle TTL 60 minutes, auto evict of one JIT model (Documented) | Idle TTL on managed engines only; external servers tolerated | Another app's JIT load can evict an LM Studio model; the router treats it as a crash or slow load |
| OpenAI compatible local server (Documented) | One `OpenAICompatProvider` for SGLang, `mlx_lm.server`, LM Studio, and cloud | Cloud egress requires the durable `inference.cloud` grant (R7) |
| Behavior under concurrent external load (Unknown) | Slot counts are configuration | Measured in the opt in live smoke, not assumed |

## Mapping to deliverables

- Evidence and method: `../research/bionic/deep-analysis.md`.
- Prior translation and the persistence taxonomy: `discord-to-gameshark.md`.
- Invariants a draining lease must preserve: `continuity-invariants.md`.
- Inference router design built on these mechanisms: `inference-router.md`.
