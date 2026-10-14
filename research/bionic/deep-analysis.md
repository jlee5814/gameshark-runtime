# Bionic (LM Studio) deep analysis for GameShark persistent agents

Bionic.app is LM Studio rebranded and extended by its vendor, Element Labs. This study replaces the planned LM Studio investigation and keeps its questions: model lifecycle, inference routing, memory and resource management, and runtime orchestration.

Read only static analysis of an authorized local install. No auth bypass, no anti tamper defeat, no secret extraction, no modification of the application. The app was not launched by this analysis (see Integrity note).

## Evidence and confidence legend

- **Observed**: read directly from a shipped artifact on this machine. Anchor cited. Observation is static: no Observed claim is runtime verified.
- **Documented**: stated in the vendor's public documentation. Corroborates intent; does not prove this build behaves so.
- **Inferred**: derived from observed code plus Electron and Node semantics. Not executed.
- **Unknown**: not resolvable from static evidence; stated so it is not mistaken for absence.

Anchors name identifiers and literal strings, which stay greppable in the shipped bundle. Byte offsets refer to the decoded scratch copy and are not stable across builds.

## Target identity

| Evidence | Value | Meaning |
|---|---|---|
| `CFBundleIdentifier` | `ai.elementlabs.bionic` | Element Labs is LM Studio's company |
| Signer | Developer ID Element Labs Inc (`D65G88RHWN`), notarized, hardened runtime | genuine vendor build |
| `Resources/app/package.json` | `name: lm-studio`, `productName: Bionic`, `author: LM Studio <team@lmstudio.ai>`, `1.1.8+10` | same codebase, new product name |
| `app-update.yml` | bucket `bionic-updaters`, `updaterCacheDirName: lm-studio-updater` | shared updater lineage |
| Main bundle | `.webpack-bionic/main/index.js`, 33 MB, SHA256 `bfc361dd…647db2` | all orchestration logic |
| User state root | `~/.lmstudio/` shared with LM Studio and `llmster`; Bionic adds `~/.lmstudio/apps/bionic/` | shared engines, models, settings |
| Product strings | `Load this model through LM Studio`, `Quit LM Studio completely (or stop llmster)` | Bionic coexists with LM Studio on one machine |

Verdict (Observed): Bionic is LM Studio's engine and orchestration core with an agent layer on top (`bundled-skills/`, Deno, Pyodide, browser control, LM Link device mesh).

## Method and limits

1. REA `inspect_electron_fuses` (Evidence `ev_a2a3451a…`): `RunAsNode` on, `EnableNodeCliInspectArguments` on, ASAR integrity validation off, `OnlyLoadAppFromAsar` off. The app ships as a plain directory, not an ASAR.
2. REA `analyze_javascript_application` (Evidence `ev_de97ee93…`) exhausted a 4 GiB worker heap on `index.js`; graph coverage is partial and recovered no IPC or process facts. REA could not answer the questions alone.
3. The bundle uses a javascript obfuscator string table: a 56,034 entry array, rotated by a `parseInt` checksum loop, read through `a0_0x334d(index)` with offset `0x188`. I reimplemented the decoder in Python, solved the rotation statically (181 shifts), and resolved only calls through proven aliases of the decoder. The bundle was never executed. An early pass also rewrote webpack `require(0x…)` calls; I caught the false positive and restricted substitution to the 44,802 decoder aliases.
4. Native layer: `nm`, `otool`, and backend manifests under `~/.lmstudio/extensions/backends/`.

### Integrity note

`codesign --verify --deep --strict` now fails: `a sealed resource is missing`, under `bin/extensions/backends/vendor/`. Cause (Observed): on first launch, `unbundleEnginesAndVendorLibs` moves bundled engines from the signed bundle into `~/.lmstudio/extensions/backends` (log string `Finished unbundling … from the app installer`). The whole `~/.lmstudio` tree, the moved directories, and `~/Library/Application Support/Bionic` carry a first launch timestamp of 2026-10-14 14:07, and `app-install-location.json` records argv `/Applications/Bionic.app/Contents/MacOS/Bionic`. No command in this analysis executes the app binary; who launched it is Unknown. **The app breaks its own code seal at first run**; signature verification therefore cannot prove this analysis left the bundle untouched. The binding and bundle hashes recorded above serve as the integrity baseline instead.

## Phase 1: shell, engine, backends, updater

Four layers, each with a different lifetime:

1. **Signed shell** (Observed): Electron, a 53 KB `MacOS/Bionic` stub, four helpers, Squirrel. Immutable between app updates, except the first run unbundling above.
2. **Orchestrator** (Observed): the Electron main process (`index.js`). Owns model instances, guardrails, routing, LM Link, and every child process. Nearly all decisions live here.
3. **Bundled native observers** (Observed): `bin/liblmstudio/metal/liblmstudio_bindings.node` and `libstardust_bindings.node`. Exports are hardware survey and memory probes only: `getAvailableRAM_Mac`, `getAvailableVRAM_Mac`, `getProcessUsedRAM_Mac`, `getMachinePhysicalRAM_Mac`, `HardwareSurveyResultToJS`. Linked against Metal and IOKit. No inference code.
4. **Engines as downloadable extension packs** (Observed): `~/.lmstudio/extensions/backends/<name>-<version>/` with `backend-manifest.json`. Installed here: `llama.cpp` 2.55.0 and 2.57.0 (GGUF), `mlx-llm` 1.13.1 (SafeTensors, Python 3.11 via `_amphibian`), `executorch-asr` 0.0.8. Engines are versioned side by side; the orchestrator picks one per model format.

Two engine invocation paths exist (Observed, `loadIndexedModelRepresentation`):

- **Native worker**: the orchestrator forks `llmworker.js` under a Node child (`NodeProcessForker` on macOS and Linux; `ElectronProcessForker` otherwise). The worker loads the engine's `llm_engine.node` (exports `LLMEngineWrapper::LoadModelWithConfig`, `LoadDraftModel`, `LoadEmbeddingModel`, `UnloadDraftModel`). MLX loads route through an `AmphibianProcessForker` keyed by Python path.
- **Engine protocol runtime**: the orchestrator spawns a standalone HTTP server per model instance. Runtime kinds: `llama-server`, `vllm`, `omlx`, `openvino`, `splash`. The llama.cpp 2.57.0 manifest declares `engine_protocol_server.runtime_kind: llama-server`. Selection: `isEngineProtocolRuntimeEnabled` plus the app setting `useLlamaCppEngineProtocolRuntime3`. Decision models and Splash models require this path.

## Phase 2: the five questions

### 1. Model lifecycle: discover, load, initialize, update, terminate

- **Discover** (Observed): a model index (`modelIndexProvider`) plus a runtime index. Loads block on `longRunningTaskProvider.ensureCompletedAtLeastOnceAndNotRunning('indexingRuntimes')`, so no load races a runtime reindex.
- **Load** (Observed, `loadIndexedModelRepresentation`), in order:
  1. Policy gates: platform support, drafter models refused standalone, enterprise catalog policy.
  2. Guardrail plan (see question 3). Rejection throws `ModelLoadingGuardrailRejectionError` before any process exists.
  3. Mint an `instanceReference` (random base64) and publish the instance as `loading` with `progress: 0`, `ttlMs`, `isJIT`, `pid: null`.
  4. Resolve a layered config stack (hardware, user model default, instance) and collapse it.
  5. If `unloadPreviousJITModelOnLoad`, unload every other JIT instance first.
  6. Select backend (`backendManager.getCurrentBackendInfo(format)`) and runtime path; build the proxy object; call `modelProxyObject.load(onProgress)`.
  7. Emit `modelLoaded`. On any failure before the proxy exists, the instance row is spliced out, leaving no ghost entry.
- **Identity** (Observed): two names per instance. `instanceReference` is opaque, unique, and never reused. `identifier` is human facing; conflicts resolve by policy `bump` (append a number), `error`, or `unloadPrevious`.
- **Update** (Observed): config changes stage into `stagingLoadTimeConfigSignal`; `reloadNeededSignal` derives from the diff; `reload()` disposes the runtime and starts it again under the same instance reference. No hot patch of a running engine.
- **Terminate** (Observed, `unloadModel`): idempotent. A second call on an `unloading` instance returns. The proxy drains operations, disposes the runtime, then resolves `unloadPromise`, which detaches TTL timers and status subscribers.

### 2. Inference routing: where requests enter, how a backend and model are chosen

- **Entry points** (Observed): the OpenAI compatible and REST HTTP server (`createHttpServerBackendInterface`, `AnthropicHttpServerProvider`, `handleAnthropicMessages`), the in app session engine (`ngPredictTokens`), the `lms` CLI, and LM Link peers. All converge on `ModelLoadingProvider` instances.
- **Model resolution and JIT** (Observed, `getOrLoadInstanceReference`): under a per model mutex (`ngManagedLoadMutex.run(modelKey, …)`), reuse a `loaded` instance, else wait on a `loading` one, else load with `isJIT: true`. Load progress streams to the caller as `modelLoading` events.
- **Backend choice** (Observed, `getBestBackendOrThrow`): by model format. GGUF on Mac takes the highest installed `llama.cpp`; SafeTensors takes `mlx-llm`; ONNX, GGML, PTE have their own resolvers. A per format user preference overrides (`backendPreferences`).
- **Per instance concurrency** (Observed): each LLM proxy owns a `ParallelPredictionQueue` whose admission limit is `max(1, llm.load.numParallelSessions)` when the model and backend support parallel sessions, else 1. `vllm` runtimes admit without limit (`1/0`), delegating batching to vLLM. Queue length publishes to `processingState.queued`.
- **Cross device routing** (Observed, `findLMLinkPeerRoute`): LM Link selects the first network with the target `deviceIdentifier` online, connectable, holding the required capability and outgoing permission, then dials a WebSocket through that network's `LmlinkInstance`. Config from a remote peer is filtered with `requiresPrivilegedConfigWrite`, so a peer cannot set privileged load fields.
- **Engine protocol transport** (Observed): spawned servers bind `127.0.0.1` on an untried port with a per spawn random 32 byte API key; HTTP timeout `ENGINE_PROTOCOL_HTTP_REQUEST_TIMEOUT_MS = 30000`.

### 3. Memory and resource management: who owns the budget, what evicts

- **Owner** (Observed): the orchestrator, through `ModelLoadGuardrailPlanner.createPlan`. Engines report usage; they do not admit.
- **Plan types** (Observed): `bypass` (user bypass, or vLLM in config file mode), `engine-owned` (Splash, Torch SafeTensors: the engine sizes itself), `runtime-auto-fit` (GGUF with `llama.autoFit` on a `llama-server` new enough for `autoFitResourceLimits`: the server receives resource limits and fits layers itself), `legacy` (orchestrator estimates and checks).
- **Legacy check** (Observed, `checkModelUnderGuardrails`): estimate model plus context bytes (recommendation provider, or file size times 1.4 as fallback), add current usage excluding instances already unloading, and compare to a threshold. `canLoad = current + estimate <= threshold`.
- **Thresholds** (Observed): `estimateTotalMemory` uses RAM alone on unified memory, RAM plus VRAM otherwise. Modes apply a layered ratio:

  | Mode (stored as) | First 16 GiB | Remainder | Apple AutoFit device reserve |
  |---|---|---|---|
  | strict (`low`) | 65% | 85% | 4 GiB |
  | balanced (`medium`) | 75% | 85% | 2 GiB |
  | relaxed (`high`) | 85% | 90% | 0 |
  | custom | fixed bytes | | total minus custom |
  | off | unlimited | | |

  Live GPU fill cap `LIVE_VRAM_MAX_FILL_RATIO = 0.85`. Session managed models may be limited to one resident at a time (`enforceSingleNGManagedModel`).
- **Eviction** (Observed): no LRU under memory pressure. Two mechanisms only. (a) Idle TTL: a timer rearmed on every `lastUsedTimeSignal` change fires `unloadModel` at `lastUsed + ttlMs`. JIT loads inherit `developer.jitModelTTL` when no explicit `ttlMs` is given; explicit loads never expire unless asked. (b) Single slot JIT: `unloadPreviousJITModelOnLoad` unloads other JIT models before a new JIT load. Explicit loads are never evicted to make room; the guardrail refuses instead.
- **Inference (Inferred)**: refusing rather than evicting keeps user pinned models stable and makes failure loud. The cost: a JIT request can fail on a machine where an idle explicit model holds memory.

### 4. Recovery: worker crash, startup failure, update failure, OOM

- **Model runtime crash** (Observed): both proxy kinds treat process death as terminal for the instance. Native worker: on `last-child-exit`, set `unloadReason = {type: 'crashed', exitCode}` and resolve `unloadPromise`. Engine protocol: `handleRuntimeProcessExit` logs `exited unexpectedly`, cancels all operations, disposes, and resolves. **No automatic restart of a model instance.**
- **Recovery at the demand edge** (Observed): the session engine maps an unload during prediction to `NGRetryableError('The local model was unloaded. Retrying with fresh settings.')`, unless the unload was explicit (`isExplicitModelUnloadError`). The retry passes through `getOrLoadInstanceReference`, which loads a fresh instance under the per model mutex.
- **Startup failure** (Observed, `EngineProtocolRuntimeManager.startRuntime`): at most 3 attempts. Each failure before health yields a diagnostic; the driver's `getStartupRetryPlan` returns a changed plan (new port, or a recovered load config) or null, and null stops retrying. Config the runtime rewrote during recovery is recorded in `recoveryOriginalValues`, so the drift from the request stays visible. A process that exits immediately after health is a hard error. A dedicated exit code (`resourceGuardrailFailureExitCode`) reports an AutoFit refusal as a resource rejection, not a crash.
- **Infrastructure restart** (Observed): only the system resources observer self restarts: `maxRestartAttempts = 3`, `restartDelayMs = 1000`, unexpected exit notifications throttled to one per 10 minutes.
- **Shutdown** (Observed): `CleanupProvider` registers ordered disposers, including `Stop engine protocol runtimes` and `Terminate child processes`; `disposeAllRuntimes` marks the manager shut down so no new runtime can start mid teardown.
- **Update failure** (Unknown): app update uses Squirrel plus differential downloads; backend packs install side by side and old versions persist (two llama.cpp versions present). I did not trace backend update rollback.

### 5. Orchestration continuity: what survives restart

- **Survives** (Observed): engines and vendor libraries, models, presets, per model default configs, settings, skills, chat and session history packs (`apps/bionic/.internal/api-prediction-history/packs`, evicted by time: `EVICTION_INTERVAL_MS = 86400000`, `EVICTION_MIN_INTERVAL_SEC = 21600`, `MAX_NODES_PER_PACK = 1000`).
- **Does not survive** (Observed): loaded model instances. Instance state lives in memory signals only (`modelInstancesSignal`). After restart, loaded models reappear through JIT on first request, or `autoLoadSelectedModel` for the UI selection.
- **Cross process ownership** (Observed, `LocalInferenceHandoff`): Bionic, LM Studio, and `llmster` share one `~/.lmstudio`. Exactly one process owns local inference and shared pack writes:
  - The lease is an open SQLite write transaction on `.internal/local-inference-owner.sqlite`: `BEGIN IMMEDIATE`, never committed, held for the life of the owner. Release is `ROLLBACK`. On process death SQLite and the kernel release it, with no TTL and no heartbeat.
  - Non owners hold a read transaction on the sibling `.packs` database (`BEGIN; SELECT * FROM sqlite_schema`), protecting shared packages they use. Pack mutation requires `BEGIN EXCLUSIVE` on `.packs` and fails fast with `SharedPacksInUseError`.
  - Phases: `blocked`, `starting`, `local`, `stopping`, `flushing`, `hosted`, `failed`. `canWrite` is lease held and not `flushing`. Handoff to a starting host: stop the backend, await in flight mutations, enter `flushing`, flush, protect packs as a reader, then `ROLLBACK`. Waiters poll `acquire()` every 100 ms.
  - Discovery is separate from ownership: `local-inference.json` advertises a running host; ownership is the lock alone.

## Documented cross check

Added 2026-10-14 from LM Studio public documentation. No app launch, no dynamic analysis; the bundle's code seal remains broken (see Integrity note), so runtime capture stays deferred.

| Documented statement | Source page | Agrees with |
|---|---|---|
| JIT loaded models default to a 60 minute idle TTL, reset on each request, settable through a `ttl` request field | Idle TTL and Auto Evict | Observed idle TTL timer and `developer.jitModelTTL` (question 3) |
| Auto evict keeps at most one JIT loaded model resident; explicitly loaded models are unaffected | Idle TTL and Auto Evict | Observed `unloadPreviousJITModelOnLoad` and no eviction of explicit loads |
| The server exposes `GET /v1/models`, `POST /v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/responses`, `/v1/decisions`; examples use port 1234 | OpenAI Compatibility Endpoints | Observed HTTP entry points (question 2); `/v1/decisions` corroborates the observed decision models |
| An Anthropic compatible surface exists (navigation entry) | OpenAI Compatibility Endpoints | Observed `AnthropicHttpServerProvider` |

Documentation describes LM Studio; Bionic shares its codebase (Observed) but may diverge per build. Documented rows do not upgrade any Observed claim to runtime verified.

## Observed versus inferred summary

| Claim | Confidence | Anchor |
|---|---|---|
| Bionic is LM Studio rebranded by its vendor | Observed | bundle ID, signer, `package.json` |
| Inference engines are side by side downloadable packs | Observed | `backend-manifest.json`, two llama.cpp versions |
| Two runtime paths: forked native worker, spawned engine protocol server | Observed | `llmworker.js`, `EngineProtocolRuntimeManager` |
| Admission before spawn via guardrail plan | Observed | `ModelLoadGuardrailPlanner.createPlan` |
| Guardrail ratio tables | Observed | layered usability constants |
| Eviction only by idle TTL and single slot JIT | Observed | `lastUsedTimeSignal` timer, `unloadPreviousJITModelOnLoad` |
| No eviction of explicit models under pressure | Observed (absence within decoded loader) / Inferred (global) | loader and guardrail paths; other callers not exhaustively traced |
| Crash means unload, never restart, for model instances | Observed | `unloadReason type crashed` in both proxies |
| Recovery happens on next demand | Observed | `NGRetryableError`, `getOrLoadInstanceReference` |
| Startup retry bounded at 3, plan driven | Observed | `startRuntime` loop, `getStartupRetryPlan` |
| Crash safe lease as held SQLite transaction | Observed | `LocalInferenceHandoff.acquire` |
| App breaks its own code seal on first run | Observed | `unbundleEnginesAndVendorLibs`, codesign failure |
| Backend update rollback | Unknown | not traced |
| Runtime behavior under real load | Unknown | no model on disk; no runtime capture |

## What did Bionic teach us about persistent agents that we did not already know?

1. **A lease can be a held transaction instead of a renewed row.** Bionic never writes an owner record. It holds `BEGIN IMMEDIATE` open for its whole life, so liveness is the lock itself and death releases it at once. GameShark already reached half of this: `InstanceLock` uses `flock` so a restart on the same host skips the TTL wait. Bionic adds what we lacked, a **drain phase**: the owner enters `flushing`, refuses new writes while still holding the lease, awaits in flight mutations, then releases. GameShark's `release` today just expires the row. Ownership transfer should be a protocol (freeze, drain, release) rather than a lock swap. Bionic has no fencing generation; GameShark's fence remains necessary because our lease spans hosts and Bionic's does not.

2. **Worker death is a state transition, and restart belongs at the demand edge.** Bionic never restarts a crashed model process. It marks the instance `crashed`, resolves its unload promise, and lets the next request rebuild it under a per key mutex with fresh settings. Explicit unloads are excluded from retry. For GameShark this splits two things we conflated in the supervisor watchdog: the resident worker, disposable and lazily rematerialized, versus the responsibility, durable and never dropped. A crash should cost one rematerialization on next due work, not a restart loop.

3. **Admit before you spawn, refuse rather than evict, and make retries change something.** Bionic decides resource fit before any process exists and fails with a typed rejection. Its startup retries are bounded at three and each retry must carry a new plan (port, config); a null plan stops the loop, and config drift is recorded against the original request. GameShark should gate responsibility acceptance on a budget check with a typed refusal, and every adapter retry should name what it changed, journaling the original intent so recovery can tell a degraded run from the requested one.

The non obvious part: Bionic keeps almost nothing alive. Loaded models, workers, and runtime servers all die with the process, and the system stays correct because every one of them rebuilds from durable config on demand. The only thing it guards with a lock is the right to write shared state. GameShark's equivalent apex is durable responsibility ownership; everything below it should be as cheap to lose as a Bionic model instance.
