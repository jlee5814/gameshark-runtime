# GameShark Runtime v0.2 (prototype)

A clean room, deterministic prototype that proves an agent named Stark can lose his
process, restart from durable SQLite records, recover his responsibilities, and resume
cross environment work without silently losing commitments or duplicating externally
observable effects. v0.2 adds a real, read only GitHub monitoring responsibility that
survives restarts without new prompts. No LLM, no game client.

Design sources: `../architecture/agent-supervisor.md`, `../architecture/continuity-invariants.md`.

## The question, answered by tests

> Can Stark lose his process, restart from durable records, recover his
> responsibilities, and resume cross environment work without silently losing
> commitments or duplicating externally observable effects?

Yes, under the mock destination assumptions documented below. The real subprocess kill
test (`test_real_subprocess_kill_then_recover`) sends `SIGKILL` to a child process after
it commits an external effect, then recovers in the parent from SQLite alone: same uuid,
one external comment, no duplicate.

## Architecture

```
          external events                         consequential effects
 Game sim ───────────────▶ ┌───────────────────────────┐ ───────────────▶ Game sim (own db, fenced)
 GitHub   ───────────────▶ │       AgentSupervisor      │ ───────────────▶ GitHub  (own db, fenced)
                           │  EventRouter (durable inbox)│
                           │  AdapterRegistry + GrantStore (durable authz)
                           │  EffectJournal (intent/outcome/reconcile)
                           │  ResponsibilityStore        │
                           │  LeaseManager (fenced owner) │
                           │  Identity (assign once)      │
                           └──────────────┬──────────────┘
                                          │ all writes guarded by lease generation
                                   runtime.db (SQLite, WAL, synchronous=FULL)
```

Durable truth lives only in SQLite. A supervisor instance is one process lifetime;
recovery is a pure function of the database.

## SQLite schema (runtime.db)

```sql
CREATE TABLE identity (agent_id TEXT PRIMARY KEY, uuid TEXT, created REAL);
CREATE TABLE lease (agent_id TEXT PRIMARY KEY, owner_token TEXT, generation INTEGER, expires_at REAL,
                     state TEXT);   -- held | draining | released
CREATE TABLE grants (agent_id TEXT, adapter TEXT, cap TEXT, granted INTEGER, updated REAL,
                     PRIMARY KEY (agent_id, adapter, cap));
CREATE TABLE responsibilities (id TEXT PRIMARY KEY, agent_id TEXT, kind TEXT, state TEXT,
                     payload TEXT, checkpoint TEXT, created REAL, updated REAL);
CREATE TABLE events (event_id TEXT PRIMARY KEY, adapter TEXT, type TEXT, payload TEXT,
                     status TEXT, delivered_count INTEGER, created REAL);
CREATE TABLE effects (key TEXT PRIMARY KEY, agent_id TEXT, generation INTEGER, status TEXT,
                     external_id TEXT, intent TEXT, created REAL, updated REAL);
```

Each remote (`game.db`, `github.db`) keeps its own `moves`/`comments` plus a `fences`
ledger storing the highest fence accepted per operation.

## State machines

Agent:

```
cold --load identity--> claimed --acquire fenced lease--> active
active --reconcile effects + replay inbox--> running
running --consequential action--> effecting --journal outcome--> running
running --SIGTERM--> draining --in flight work done--> released
draining --drain deadline--> cancelled at a safe boundary --> released
running | draining --crash--> (discard) --> cold (restart)
```

Responsibility:

```
pending --activate--> active --complete--> done
active --unreconcilable outcome--> blocked (escalation)
```

Effect (journal row):

```
(none) --intent--> pending --outcome--> done
pending --probe UNKNOWN--> blocked
```

## Crash recovery sequence (the C case: crash after remote, before local journal)

```
supervisor1        effect journal        GitHub (remote)        supervisor2 (restart)
    |  submit pr        |                      |                       |
    |------------------>| write intent=pending |                       |
    |                   |--------------------->| post_comment(fence=1) |
    |                   |                      |  comment stored (1)   |
    |     *** SIGKILL before writing done ***  |                       |
    |                   |                      |                       |
    |                   |                      |   recover(): probe    |
    |                   |                      |<----------------------| probe(op_key)
    |                   |                      |   FOUND(op_key)------->|
    |                   |                      |                       | write done, no re-post
    result: exactly one comment, effect done, no duplicate
```

## Invariant to test coverage

| Invariant (continuity-invariants.md) | Tests |
|---|---|
| I1 identity stability | `test_i1_identity_stable_across_restart`, `test_i_model_replacement_keeps_identity`, `test_real_subprocess_kill_then_recover` |
| I2 single owner | `test_i2_single_owner`, `test_f_two_concurrent_supervisors` |
| I3 checkpoint before effect | `test_i3_checkpoint_before_effect`, `test_b_crash_after_intent_before_dispatch` |
| I4 idempotent effect | `test_i4_idempotent_effect`, `test_c_crash_after_remote_before_journal`, `test_delayed_remote_visibility` |
| I5 at least once plus dedup | `test_i5_inbox_replay_and_dedup`, `test_a_crash_before_intent`, `test_d_crash_after_journal_done` |
| I6 authorization never widens, revocation durable | `test_i6_authorization_durable`, `test_authorization_revocation_survives_restart` |
| I7 outcome completeness | `test_i7_outcome_completeness` |
| escalation on ambiguity (I4 refinement) | `test_h_ambiguous_blocks_and_escalates` |
| fencing on durable writes | `test_g_stale_worker_cannot_commit` |
| destination side fencing | `test_stale_external_effect_rejected_by_destination` |

## Acceptance criteria to test

| Criterion | Test |
|---|---|
| Stark uuid stable after full restart | I1 tests + subprocess kill |
| Both standing responsibilities survive | `test_e_two_outstanding_responsibilities` |
| Safe operations resume automatically | A, B, C, E |
| Confirmed completed operations not duplicated | C, D, delayed visibility, subprocess |
| Ambiguous operations blocked and escalated | H |
| Stale worker cannot commit local state | G |
| Revoked permissions stay revoked | I6, revocation test |
| Model replacement keeps identity | I |

## Proven vs unproven

| Property | Proven here | Assumption it rests on | Unproven for real apps |
|---|---|---|---|
| Local state exactly once | Yes, fencing on every durable write | single SQLite writer, atomic `BEGIN IMMEDIATE` | multi host needs a real lease service |
| External effect exactly once (consequence) | Yes, in the mock | destination deduplicates by operation id OR a reliable probe | a real API may lack idempotency keys or a probe |
| Stale worker produces no external effect | Yes, in the mock | destination enforces a fence per operation | a real API that ignores fences cannot give this |
| Ambiguous outcome never guessed | Yes | probe can return an explicit UNKNOWN | a real API may hide ambiguity behind a timeout |
| Identity and authorization survive restart | Yes | durable SQLite | key custody and secret rotation out of scope |

Key correctness point (review issue 3): local fencing stops a stale worker from
committing **local** state, but a stale worker that already dispatched an external
action cannot be stopped by local fencing. Preventing the stale **external** effect
requires the destination to enforce the fence. `test_stale_external_effect_rejected_by_destination`
demonstrates the destination rejecting an old fence; without that destination guarantee,
the property does not hold.

## Known limitations

- Single host, single SQLite file. No distributed consensus; the lease is local.
- Determinism by design: tests use no real network and no clocks beyond an injectable one;
  no LLM. v0.2 adds an opt in live GitHub transport.
- The probe UNKNOWN path blocks and escalates but does not implement an operator
  resolution workflow; a blocked effect stays blocked.
- An unauthorized event stays pending (refused, not dropped, not executed); no dead
  letter queue yet.
- `git diff` style module hot swap and multi agent ownership are future work.

## Reproduce

```sh
cd /Users/jaebonglee/Projects/gameshark-research/prototype
uv venv --python 3.13
uv pip install pytest
uv run pytest -v                 # all 316 tests (v0.1, v0.2, fencing, draining, expiry, v0.3 steps 1 to 5)
uv run python demo.py            # v0.1 narrated Stark run with a real SIGKILL
uv run python demo_v2.py         # v0.2 PR monitoring across a real SIGKILL and a revoke
uv run python companion.py <runtime_db>   # render the companion feed for a run
uv run python stark.py --help    # the persistent companion CLI (see v0.2 below)

# v0.2 critical tests
uv run pytest -v tests/test_daemon.py::test_responsibility_survives_sigkill_without_second_prompt
uv run pytest -v tests/test_github_monitor.py::test_review_with_lower_id_than_prior_comment_is_not_lost
uv run pytest -v tests/test_github_monitor.py::test_github_auth_revoked_blocks_notifies_once_and_resumes_explicitly
uv run pytest -v tests/test_github_rest.py

# lease fencing: stale owner exits and commits nothing
uv run pytest -v tests/test_lease_fencing.py
uv run pytest -v tests/test_takeover.py

# graceful draining and fenced handoff
uv run pytest -v tests/test_lifecycle.py
uv run pytest -v tests/test_drain.py

# inference journal: crash points E1 to E4, fencing, races
uv run pytest -v tests/test_inference_journal.py

# inference router: policy, budgets, slots, failover, 1,000 call soak
uv run pytest -v tests/test_inference_router.py
uv run python bench/router_overhead.py

# OpenAI compatible provider: SSE, usage, cancellation, locality, redaction, E1 to E4
uv run pytest -v tests/test_openai_compat.py
uv run python bench/openai_compat.py

# model lifecycle tracker: lazy readiness, crash versus failure, cooldown, replacement
uv run pytest -v tests/test_model_tracker.py

# individual critical tests
uv run pytest -v tests/test_recovery.py::test_real_subprocess_kill_then_recover
uv run pytest -v tests/test_recovery.py::test_c_crash_after_remote_before_journal
uv run pytest -v tests/test_recovery.py::test_h_ambiguous_blocks_and_escalates
uv run pytest -v tests/test_recovery.py::test_g_stale_worker_cannot_commit
uv run pytest -v tests/test_recovery.py::test_stale_external_effect_rejected_by_destination
uv run pytest -v tests/test_recovery.py::test_authorization_revocation_survives_restart
```

## v0.2: persistent real GitHub responsibility

Stark accepts a durable `monitor_pull_request` responsibility with one prompt and keeps it
against real GitHub across process kills, restarts, offline periods, rate limits, and
token revocation, with no further prompts. Read only, deterministic, no LLM. Full design,
failure matrix, and limitations: [`../architecture/github-monitor.md`](../architecture/github-monitor.md).

### Use it

```sh
uv run python stark.py watch jlee5814/gameshark-runtime#1   # the one prompt
uv run python stark.py run --interval 60 --notify           # serve; restart freely
uv run python stark.py status                               # uuid, lease, duties, backoff
uv run python stark.py feed                                 # unread updates
uv run python stark.py ack --all
uv run python stark.py resume jlee5814/gameshark-runtime#1  # after fixing a refused token
```

Token: `GITHUB_TOKEN`, else `gh auth token`. Prefer a fine grained, read only token
(Pull requests, Checks, Metadata). State: `~/.gameshark/runtime.db` (`--db` overrides).
`watch` treats activity older than the watch as history; `--backfill` delivers it.

### Components

| Layer | Module | Owns |
|---|---|---|
| identity, ownership | `gameshark/identity.py`, `lease.py` (v0.1, unchanged) | uuid, fenced generation |
| durable responsibilities | `gameshark/responsibilities.py` (unchanged) | active, blocked, done |
| execution worker | `gameshark/worker.py`, `stark.py` | scheduling, lease renewal, operator requests, host lock |
| environment adapter | `sims/github_monitor.py` | seen set, backoff, audit, notification effects |
| transport | `gameshark/github_client.py` | GET only REST, tail first pagination, ETags, typed errors |
| pure logic | `gameshark/github_events.py`, `classify.py` | logical event ids, relevance |
| companion | `companion.py` | feed, unread state, banner hook |

### What changed from the first v0.2 scaffold

| Gap | Fix |
|---|---|
| nothing ran between prompts | `stark run` daemon resumes every active responsibility at start |
| one integer cursor across review ids and comment ids hid reviews | per stream logical ids and a durable seen set |
| no CI, no inline comments | check runs on the head commit, pull request review comments |
| README claimed pagination the code lacked | Link pagination; ETags only for single page streams |
| pagination stopped after 20 pages and never reached newer reviews (#4) | tail first walk that stops at a fully seen page, durable backfill cursor, one warning per truncation episode |
| 401 or 403 crashed the poll; client slept in place | typed errors, durable backoff, blocked responsibility with one notice |
| every comment counted as noise | self, bot, acknowledgement, and inline rollup rules |

### Failure handling

| Condition | Behavior | Survives restart |
|---|---|---|
| 5xx, timeout, DNS, refused | backoff 30 s doubling to 15 min, no notice | yes, `next_poll_at` |
| primary or secondary rate limit | wait until reset or `retry-after`; notice once if over 15 min | yes |
| 401, 403 without rate limit, 404 | responsibility blocked, one notice per episode, no polling | yes, until `resume` |
| checks forbidden for the token | reviews and comments continue; status reports CI unavailable | yes |
| offline for hours | next poll replays everything unseen; one catch up notice over 1 h | yes |
| PR merged or closed | notice, responsibility done, polling stops | yes |
| local `github.read` revoked | polling stops, responsibility intact | yes |

### Live soak (2026-10-14, real GitHub, GET only)

Stark watched this repository's PR #1 with the gh token:

1. `watch jlee5814/gameshark-runtime#1 --backfill`, then `run --interval 5`.
2. First poll found a `chatgpt-codex-connector[bot]` review summary comment:
   audited `bot`, not delivered.
3. The daemon received SIGKILL while the PR's CI run was pending. `status` showed
   `daemon not running` and the lease still held until TTL.
4. CI completed on GitHub while Stark was dead.
5. `run` again with no other command: same uuid `443db68b...`, same lease generation,
   `resuming 1 responsibilities`, the CI result detected on the first tick and audited
   `ci`, `CI test concluded success`, not delivered.
6. Totals: 2 inbox events, both acknowledged, 0 notifications, 0 duplicates. SIGTERM
   stopped the daemon cleanly.

### Live actionable event soak (2026-10-14, real GitHub)

A deliberately failing test on throwaway draft PR #2 produced a real failing check run.
The full chain ran: GitHub event, poller, classifier, durable notification, companion
feed, acknowledgment.

| Step | Action | Observed |
|---|---|---|
| a | `watch jlee5814/gameshark-runtime#2`, `run` | responsibility active, feed empty |
| b | SIGKILL while CI pending | daemon not running, lease held |
| c | CI fails on GitHub while Stark is dead | check run 116009057415, `failure` |
| d | bare `run` restart | first tick: audit `ci_failure` relevant, effect `done`, inbox event acked once, feed row 1 `[ci_failure] test failure` with the run URL |
| e | SIGKILL before ack, restart, six polls | same single unread row 1, one notification, inbox delivered_count 1 |
| f | `ack 1`, SIGKILL, restart, six polls | `feed` empty, `feed --all` shows row 1 read, still one notification |
| g | close PR #2 without merging | `[pr_state] pull request closed`, responsibility `done`, the closing comment audited `self` |
| h | SIGTERM | clean stop, lease released |

Identity `04e6103f...` and lease generation 2 held across all four process lifetimes.
PR #2 is closed unmerged and its branch deleted. The automated twin of this run is
`test_daemon.py::test_actionable_event_survives_restarts_until_acked`.

### Guarantees against real GitHub

| Property | Holds? | Basis |
|---|---|---|
| no write to GitHub | yes | GET only primitive; tested on the wire and in source |
| duty survives restart with no prompt | yes | responsibilities drive polling; subprocess SIGKILL test and live soak |
| no duplicate notification | yes | inbox dedup by logical id, journal plus `op_key` dedup |
| no missed review, comment, or check run | yes for reviews and comments at any length; check runs up to 2,000 per commit, with a warning past that | seen set over paginated lists, no id order assumption; tail first pagination with a durable backfill cursor; a capped read enters the feed as a system warning |
| backoff and blocks survive restart | yes | durable `next_poll_at`, `status`, `episode` |
| timeliness | no | polling interval plus GitHub eventual consistency |

Unproven: legacy commit statuses, check runs on superseded commits, multi host
ownership (lease takeover is tested between owner processes on one host), and long soak behavior beyond the session above.

## Graceful drain and handoff

Implements the `draining` lease state from `../architecture/bionic-to-gameshark.md`
(change 1) and step 0 of `../architecture/inference-router.md`. No router or provider code.

| Step | Mechanism |
|---|---|
| Freeze | SIGTERM sets `stop`; the heartbeat moves the lease and the lifecycle to `draining`. `submit` raises `Draining` before anything persists; no new tick, control request, or poll target starts |
| Drain | in flight work finishes under the still valid generation while a heartbeat thread, on its own SQLite connection, keeps renewing every TTL/3 |
| Cancel | at `--drain-deadline` (default 10 s) a cancel token stops work at the next safe boundary: before an event dispatches, between poll targets, before a detected event is submitted. Never between an effect's intent and outcome. Exit 5 |
| Abort | 2 s past the deadline, non cooperative work (a hung GET) loses: the heartbeat releases the lease and ends the process. Exit 6 |
| Release | `release` sets `released`, clears the owner, and keeps the generation |
| Fence | `guard_in_txn` rejects a missing lease row, a stale generation, a released lease, and an expired lease, so the old generation commits nothing even before a successor appears. A successor acquires generation + 1 only after release or expiry; a live draining lease refuses it |
| Resume | unchanged v0.1 recovery: pending effects reconcile by probe, pending events replay, unseen activity is detected again; `op_key` and effect keys absorb repeats |

Exit codes of `stark run`: 0 drained, 3 another owner, 4 lost ownership, 5 drain
cancelled at the deadline, 6 drain aborted at the grace limit.

Expiry: `guard_in_txn` rejects any write once `expires_at <= clock.now()`, read inside
the write's own `BEGIN IMMEDIATE` transaction, so an expired owner commits nothing even
when no successor has acquired. `renew` refuses an expired lease too, so the owner learns of
the loss at its next heartbeat or write and exits 4. A late completion (an effect whose
remote call outlived the lease) raises `FencedError` at its outcome checkpoint: the effect
stays `pending` and its event unacknowledged, and the successor reconciles it by probe.

The guarantee holds at the guard instant. SQLite holds the write lock from `BEGIN
IMMEDIATE` to `COMMIT`, so no acquire, renew, or release interleaves: a write whose guard
passed commits before any successor's acquire even if the clock crosses `expires_at` in
between, and that acquire then bumps the generation. Covered by
`tests/test_expired_lease.py`.

## Inference provider contracts (v0.3 step 1)

Implements step 1 of `../architecture/inference-router.md`. Contracts and a fake only: no
HTTP transport, journal, router, or model loading, and no change to existing runtime paths.

| Module | Contents |
|---|---|
| `gameshark/inference/types.py` | frozen `Capabilities`, `Message`, `ProviderRequest` (stable `call_id`, `attempt`, full context, monotonic deadline, required capabilities, privacy), `Chunk`, `ProviderResult` with `Usage`, `Health`, and the typed error taxonomy |
| `gameshark/inference/provider.py` | `Provider` and `ProviderCall` protocols, `complete()` |
| `gameshark/inference/fake.py` | `FakeProvider`: deterministic text, scripted delays, faults at chosen chunk indices, cancellation, deadlines, configurable capabilities and health, invocation counts |

Completion is request scoped. `generate` returns a `ProviderCall` that owns its chunks,
usage, and terminal error; the provider holds no result slot, so concurrent calls cannot
cross. Cancellation reuses `lifecycle.CancelToken`; `ProviderCancelled` subclasses the
existing `Cancelled`, so drain code catches it unchanged.

## Inference journal (v0.3 step 2)

Implements step 2 of `../architecture/inference-router.md`: five new tables
(`conversations`, `messages`, `inference_calls`, `budget_ledger`, `agent_budgets`) and
`gameshark/inference/journal.py`. Still no HTTP transport, router, executor, or model
lifecycle; `FakeProvider` stays free of SQLite.

Each attempt costs two fenced writes, the effect journal's shape:

1. **Reserve** bumps `inference_calls.attempts` and inserts a `budget_ledger` row
   `reserved` before the provider sees a byte. The request comes from `messages` rows with
   `seq < base_seq`, never from the caller, so no provider ever supplies identity or history.
2. **Commit** inserts the assistant message at `seq = base_seq`, advances `next_seq`, marks
   the call `done`, and settles the ledger to actual usage, all in one `BEGIN IMMEDIATE`
   transaction guarded by `guard_in_txn`.

Before commit, `collect` checks every chunk and the result against the reservation's
`call_id` and attempt, with contiguous indices and text equal to the joined chunks;
any mismatch raises `ResultMismatch` and writes nothing. Commit fails closed on a stale,
released, or expired lease (`FencedError`), a superseded attempt (`StaleAttempt`), an
operator cancelled call (`CallCancelled`), or a moved conversation (`ContextConflict`). A
call already `done` returns its committed message. Attempts cap at 3 across restarts
(`AttemptsExhausted`); a refusal from `generate`, which does no I/O, undoes its
reservation, so it consumes no attempt and no budget.

Crash recovery, by crash point (`crash_at` label, or SIGKILL in `tests/inference_worker.py`):

| Point | Durable state at death | Recovery |
|---|---|---|
| E1 `E1_before_reserve` | event pending; no call row | replay reserves attempt 1 and commits it; ledger `settled` |
| E2 `E2_after_reserve` | attempt 1 `reserved`; provider never called | `settle_abandoned` marks attempt 1 `lost` at full reservation; replay runs attempt 2 |
| E3 `E3_after_provider` | attempt 1 `reserved`; answer only in memory | same as E2; the provider ran twice and a rerun may produce different text |
| E4 `E4_after_commit` | call `done`, message committed; event pending | replay finds `done` and returns the committed message with zero provider calls; the effect posts once |

Recovery settles abandoned reservations and nothing else. It never dispatches a call: the
owning event or responsibility replays and reissues the same `call_id`. Same owner
restarts on a live lease keep their generation, so recovery charges every `reserved` row
this incarnation does not hold; the commit's attempt and ledger checks then stop a same
generation zombie as well. An effect whose intent cites a `call_id` is refused
(`UncommittedOutput`) until that call is `done`, so model output reaches a consequential
action only through a committed message and the effect journal.

Not guaranteed: exactly once provider execution, exactly once billing, or identical text
after a rerun. Budget admission arrives with the router (step 3).

## Inference router (v0.3 step 3)

Implements step 3 of `../architecture/inference-router.md` on `FakeProvider` only: no HTTP
provider, worker executor, or model lifecycle. No schema change; the journal gains
optional arguments whose defaults keep E1 to E4 unchanged.

| Module | Contents |
|---|---|
| `gameshark/inference/router.py` | `InferenceRouter` (select, admit, dispatch, fail over, commit), `ProviderConfig`, `TokenBound`, `RouterPolicy`, `ProviderHealth`, `RouteResult`, `Overrun` |
| `gameshark/inference/scheduler.py` | `Scheduler`: slots per provider, bounded FIFO queues with direct handoff, per agent and per conversation limits, `Overloaded(retry_after)` |
| `gameshark/inference/budget.py` | fixed aligned windows; `admit_budget` runs inside the reserve transaction |

Selection runs four filters in order and refuses with a typed error when one empties the
set: capability (features, the `allowed` model constraint, prompt bound plus `max_tokens`
within the context window), policy (`LOCAL_ONLY` and the durable grant
`inference.cloud` gate cloud; a paid provider without a trusted token bound under a budget
needs `inference.cloud.soft_budget`), ordering (configured preference, then local first,
then cheapest cloud), and health (rate limit circuits, auth blocks, providers reporting
down).

Admission takes a slot first, then reserves. The reservation transaction rechecks the
cloud grant and checks every configured window against settled, lost, and still reserved
spend before it inserts the ledger row, so concurrent admissions on any connection
serialize on the SQLite write lock. A refusal rolls back: no attempt and no budget
consumed. `EgressDenied`, `BudgetExceeded`, and `BudgetUnenforceable` end the walk at
once, so a policy denial never becomes cloud fallback.

| Failure | Router action |
|---|---|
| retryable after dispatch (`ModelCrashed`, `StreamInterrupted`, `Timeout`, `RateLimited`, `ProviderUnavailable`, `ResultMismatch`) | attempt settles `lost` at full reservation; next distinct candidate; `RateLimited` opens a circuit for `retry_after` |
| `ProviderCancelled`, durable `CallCancelled` | terminal; no fallback |
| `AuthFailed` | terminal; attempt charged; `error_kind` records provider and config fingerprint; replay refused until the fingerprint changes |
| `CapabilityUnsupported` or `ContextTooLong` from `generate` | reservation undone; next candidate |
| other terminal | raised as is |

A committed answer records its provider and `degraded = 1` when it came from a fallback.

Token accounting. `estimate_tokens` counts whitespace words and bounds no real tokenizer.
Each provider declares a `TokenBound`: `TRUSTED_BYTES` reserves UTF 8 bytes plus template
overhead, a true upper bound only for byte level tokenizers, which the operator asserts;
`ESTIMATE` reserves the whitespace count and makes the budget soft. Actual usage always
settles unmodified, never truncated to the reservation, and an attempt above its
reservation returns an `Overrun` and appears in `InferenceJournal.overruns()`. A trusted
provider that overruns is demoted to `ESTIMATE` for the process.

## OpenAI compatible provider (v0.3 step 4)

Implements step 4 of `../architecture/inference-router.md`: `OpenAICompatProvider` in
`gameshark/inference/openai_compat.py`, stdlib `http.client` only. It reaches externally
managed SGLang, `mlx_lm.server`, the LM Studio and Bionic local server, and compatible
cloud APIs through `GET {base}/models` (health) and streaming
`POST {base}/chat/completions`. The base URL follows the OpenAI SDK convention and ends in
`/v1`. No engine spawning, lifecycle tracker, or worker executor yet.

```python
OpenAICompatProvider("sglang", "http://127.0.0.1:30000/v1", "qwen", 32768, max_concurrency=8)
OpenAICompatProvider("mlx", "http://127.0.0.1:8080/v1", "mlx-model", 8192)
OpenAICompatProvider("cloud", "https://api.example.com/v1", "model", 128000,
                     api_key=os.environ["API_KEY"], price_in_micros=150, price_out_micros=600)
```

| Concern | Behavior |
|---|---|
| Dispatch | `generate` validates and returns a call; no I/O. Connect and POST happen on first iteration. One connection per call, never pooled |
| Context | every request carries the full message list from the journal; no server side session is used |
| SSE | incremental decoder: reads split anywhere (inside UTF 8 sequences, between CR and LF), CRLF, LF, or CR line ends, comments and keepalives, multi line data, `[DONE]`; 1 MiB line and 8 MiB response caps |
| Locality | derived from the URL: only a loopback IP literal is local; `localhost` is refused; every other host is cloud and must use HTTPS. No locality argument exists |
| Egress | cloud locality puts the provider behind the router's durable `inference.cloud` grant; the provider also refuses a `LOCAL_ONLY` request before dispatch |
| Transport | redirects never followed; proxy environment variables never read; TLS verified with the default context |
| Secrets | the key appears only in the live Authorization header; error bodies, health details, and `repr` are scrubbed; `config_fingerprint` hashes it for `ProviderConfig.fingerprint` |
| Capabilities | no tools, JSON mode, or vision advertised; `needs` and tool role messages are refused before dispatch |

| Transport condition | Error |
|---|---|
| connection refused, DNS or TLS failure, reset before a response | `ProviderUnavailable` |
| past the deadline (any blocking step), 408 | `Timeout` |
| no bytes within `read_idle_s` before the deadline | `StreamInterrupted` |
| 401, 403 | `AuthFailed` |
| 429 | `RateLimited(retry_after)` from `Retry-After` (seconds or HTTP date) or `retry-after-ms`, else 5 s, capped at 300 s |
| 5xx, 3xx (redirect refused), stream error object | `ProviderUnavailable` |
| other 4xx, unrequested tool calls | `InvalidRequest` |
| wrong content type, malformed event, unknown finish reason, EOF or reset before `[DONE]`, `[DONE]` without a finish reason | `StreamInterrupted` |
| cancellation (wins over the deadline) | `ProviderCancelled` |

Cancellation and deadlines act inside network reads. Every blocking step (connect,
response headers, each read) waits at most `min(deadline - now, read_idle_s)`, and
`CancelToken.cancel` shuts the socket down from the cancelling thread, which wakes a
blocked read at once. The call keeps its own socket reference because `http.client` drops
`conn.sock` once a close delimited response begins. Partial text never reaches a result.

Missing usage. The request asks for a final usage chunk (`stream_options.include_usage`,
off with `stream_usage=False`). When none arrives, or its counts are not non negative
integers, the result carries `usage=None`; nothing is estimated. The answer still commits,
and the journal settles the ledger row `unmetered`: token columns stay NULL and
`cost_micros` equals `reserved_micros`, so `spend()` charges the full reservation, never
zero. `RouteResult.metered` is False and no `Overrun` is computed. The charge is an upper
bound only under a `TRUSTED_BYTES` bound (the server receives `max_tokens`); under
`ESTIMATE` the budget stays soft and the existing `BudgetUnenforceable` gate still applies
to paid providers.

## Model lifecycle tracker (v0.3 step 5)

Implements step 5 of `../architecture/inference-router.md`: `ModelTracker` in
`gameshark/inference/tracker.py`. It makes availability, crash, recovery, and replacement
explicit for externally managed servers. GameShark never starts, kills, restarts, or
unloads an SGLang, MLX, or LM Studio process: loading is a readiness check and unloading
only stops routing. The tracker is opt in (`InferenceRouter(..., tracker=...)`); without
one the router behaves exactly as in step 4. Worker and daemon wiring land in step 6.

Everything here lives in memory. A restart builds every configured model at `discovered`;
identity, conversations, messages, responsibilities, and the ledger stay in SQLite, which
the tracker never reads or writes.

| State | Meaning | Leaves for |
|---|---|---|
| `discovered` | configured, never probed by this process | `loading` on first demand |
| `loading` | a load plan is probing the endpoint | `ready`, next plan (`loading`), `failed` |
| `ready` | probe passed; no token served on this incarnation yet | `executing`, `crashed`, `unloaded` |
| `executing` | at least one admitted call | `idle`, or `ready` if no token streamed |
| `idle` | served a token, none in flight | `executing`, `crashed`, `unloaded` |
| `unloading` | replacement requested; admits nothing; in flight calls drain | `unloaded` |
| `unloaded` | drained (retired by a replacement) or evicted externally | `loading` on demand |
| `crashed` | a probe found the endpoint gone after it served | `loading` on demand, new incarnation |
| `failed` | three plans failed, or the crash cycle bound was hit | `discovered` after the cooldown |

Readiness has three levels, and the tracker never treats one as another:

| Level | Evidence | What it proves |
|---|---|---|
| Endpoint ready | `GET /v1/models` answers 200 | the server process accepts HTTP |
| Model available | the configured model id appears in that list | the server routes the name. LM Studio with JIT lists downloaded models that are not loaded, and `mlx_lm.server` loads on first request |
| GPU resident | a call on this incarnation streamed its first chunk | weights served a token. The OpenAI API exposes no residency query, so this is the only evidence; the tracker records it as `Readiness.RESIDENT` and never assumes it from a probe |

Lazy readiness. The first demand starts one load job per model (single flight); every
concurrent caller waits on it, bounded by `load_wait_s` and woken at once by its own
cancel token. A load cycle runs at most three plans, each materially different; a policy
that repeats a plan, or lists more than three, is refused at construction.

| Plan | Probe timeout | Delay | Accepts |
|---|---|---|---|
| `probe` | 1 s | none | model listed |
| `patient` | 10 s | 2 s | model listed (SGLang still loading weights) |
| `endpoint_only` | 10 s | 2 s | endpoint up, model unlisted (LM Studio JIT loads on the first request) |

After the third failure the model is `failed` for `cooldown_s` (300 s): routing skips it,
admission raises `ModelUnavailable(retry_after)` with no probe, and nothing reaches the
ledger. A `ready` or `idle` model unused for `verify_ttl_s` (30 s) is probed again on
demand: endpoint gone means `crashed`; model no longer listed means `unloaded` (LM Studio
auto evict), and the same demand reloads it.

Crash versus request failure. The tracker never calls a model crashed because a request
failed.

| Call outcome | Tracker action |
|---|---|
| `Timeout`, `RateLimited`, `InvalidRequest`, `AuthFailed`, `ResultMismatch` | request failure counted; state unchanged; no probe |
| `ProviderCancelled` (operator) | nothing |
| `ProviderUnavailable`, `StreamInterrupted`, `ModelCrashed` | suspicion recorded; in flight calls continue; the next demand probes first |
| probe after suspicion answers | suspicion cleared; same incarnation |
| probe after suspicion finds the endpoint gone | `crashed`; reload on that demand; new incarnation |

Bounded recovery. Each crash then reload cycle counts; a completed call resets the count.
At `max_recoveries` (3) the model goes `failed` with cooldown, so a server that loads and
dies on every call stops being retried. The durable cap of three attempts per `call_id`
still applies on top.

Replacement. `tracker.replace(new_config, drain_timeout_s)`:

1. The old record moves to `unloading`; it admits nothing new.
2. Admitted calls finish on the old configuration until `drain_timeout_s`. Calls still
   running then are cancelled through their own child `CancelToken`; the router settles
   that attempt `lost` and reruns the call on the successor, not as an operator cancel.
3. The old record moves to `unloaded`; the successor starts at `discovered` with the next
   generation. `Scheduler.configure` takes its slot count; `ProviderHealth.reset` clears
   circuits, demotion, and cached health for the name.
4. A call arriving during the drain waits up to `replace_wait_s` for the successor, then
   selects again: capability, privacy, and the egress grant are judged against the new
   configuration, so a replacement can neither bypass `inference.cloud` nor silently
   truncate context. With no wait budget it fails over to the next candidate. Either way
   no attempt or budget is consumed before dispatch.

Conversations need no migration: the next call resends the full context from SQLite. A
committed call records the tracker incarnation in `inference_calls.instance_ref`
(`name:generation.incarnation`), so audit can tell answers from different incarnations.

The tracker reuses `Scheduler` (sole owner of slots and queues), `ProviderHealth`
(circuits and auth blocks; `blocked` now separates them from the eager probe), and
`CancelToken`. Its `in_flight` count limits nothing; it only tells a replacement when the
drain finished.

### Manual live smoke (observed, not CI)

After PR #15 merged, a manual run routed a call through `OpenAICompatProvider` to an
externally managed SGLang/MLX server serving `mlx-community/Qwen3-4B-4bit`. The answer
committed durably to SQLite, and constructing a new supervisor and replaying the same
call returned the committed answer with zero provider calls. This is observed evidence
from one operator run on one host. CI never contacts a model server and does not
guarantee this behavior; the deterministic suite proves the same properties against
`FakeProvider` and the loopback fixture. The tracker itself has not yet run against a live
server.

## Actual test results

```
platform darwin -- Python 3.13.9, pytest-9.1.1, pluggy-1.6.0 -- /Users/jaebonglee/Projects/gameshark-research/prototype/.venv/bin/python3
tests/test_daemon.py::test_responsibility_survives_sigkill_without_second_prompt PASSED
tests/test_daemon.py::test_second_daemon_refused_and_cli_requests_reach_running_daemon PASSED
tests/test_daemon.py::test_token_revocation_blocks_survives_restart_and_resumes PASSED
tests/test_daemon.py::test_actionable_event_survives_restarts_until_acked PASSED
tests/test_drain.py::test_sigterm_idle_drains_releases_and_successor_fences PASSED
tests/test_drain.py::test_drain_finishes_in_flight_work_and_refuses_successor_until_release PASSED
tests/test_drain.py::test_drain_deadline_cancels_and_successor_finishes_exactly_once PASSED
tests/test_drain.py::test_non_cooperative_stall_aborts_at_grace_and_releases PASSED
tests/test_drain.py::test_sigkill_mid_drain_keeps_successor_out_until_expiry PASSED
tests/test_drain.py::test_stale_owner_completion_is_fenced_after_takeover PASSED
tests/test_expired_lease.py::test_expired_owner_with_no_successor_commits_nothing PASSED
tests/test_expired_lease.py::test_expiry_boundary_rejects_at_expires_at PASSED
tests/test_expired_lease.py::test_missing_lease_row_fails_closed[0] PASSED
tests/test_expired_lease.py::test_missing_lease_row_fails_closed[1] PASSED
tests/test_expired_lease.py::test_renewed_lease_commits_past_original_ttl PASSED
tests/test_expired_lease.py::test_draining_owner_commits_while_renewed_then_fences_on_expiry PASSED
tests/test_expired_lease.py::test_released_draining_owner_commits_nothing PASSED
tests/test_expired_lease.py::test_old_generation_fenced_after_takeover PASSED
tests/test_expired_lease.py::test_late_completion_fails_instead_of_recording_success PASSED
tests/test_expired_lease.py::test_expiry_during_transaction_commits_before_any_takeover PASSED
tests/test_github_monitor.py::test_assign_then_quiet_poll_delivers_nothing PASSED
tests/test_github_monitor.py::test_new_review_delivers_and_dedups_on_repoll PASSED
tests/test_github_monitor.py::test_classification_kinds_and_relevance PASSED
tests/test_github_monitor.py::test_review_with_lower_id_than_prior_comment_is_not_lost PASSED
tests/test_github_monitor.py::test_inline_comments_roll_up_into_their_review PASSED
tests/test_github_monitor.py::test_ci_in_progress_then_failure_then_recovery PASSED
tests/test_github_monitor.py::test_ci_cancelled_on_head_notifies_once PASSED
tests/test_github_monitor.py::test_ci_success_after_cancelled_is_recovery PASSED
tests/test_github_monitor.py::test_ci_cancelled_again_on_rerun_does_not_duplicate PASSED
tests/test_github_monitor.py::test_ci_cancelled_on_superseded_commit_stays_silent PASSED
tests/test_github_monitor.py::test_checks_unavailable_degrades_without_blocking PASSED
tests/test_github_monitor.py::test_baseline_records_history_without_notifying PASSED
tests/test_github_monitor.py::test_outage_backs_off_durably_then_catches_up PASSED
tests/test_github_monitor.py::test_rate_limit_pauses_until_reset_and_long_waits_notify_once PASSED
tests/test_github_monitor.py::test_offline_gap_catches_up_and_reports PASSED
tests/test_github_monitor.py::test_github_auth_revoked_blocks_notifies_once_and_resumes_explicitly PASSED
tests/test_github_monitor.py::test_merged_pr_notifies_and_completes_responsibility PASSED
tests/test_github_monitor.py::test_duplicate_inbox_delivery_absorbed PASSED
tests/test_github_monitor.py::test_revoke_github_read_stops_monitoring_safely PASSED
tests/test_github_monitor.py::test_crash_mid_delivery_recovers_without_duplicate PASSED
tests/test_github_monitor.py::test_real_subprocess_kill_mid_monitor_then_recover PASSED
tests/test_github_monitor.py::test_review_on_page_25_delivers_once_and_history_stays_quiet PASSED
tests/test_github_monitor.py::test_backfill_resumes_across_restart_without_duplicates PASSED
tests/test_github_monitor.py::test_rate_limit_mid_walk_backs_off_and_loses_nothing PASSED
tests/test_github_monitor.py::test_crash_while_marking_seen_never_strands_a_hole PASSED
tests/test_github_rest.py::test_snapshot_reads_every_stream_and_normalizes PASSED
tests/test_github_rest.py::test_pagination_follows_link_and_skips_etag_for_multipage PASSED
tests/test_github_rest.py::test_etag_304_survives_and_yields_no_items PASSED
tests/test_github_rest.py::test_http_failures_map_to_typed_errors[401-headers0-body0-AuthRevoked] PASSED
tests/test_github_rest.py::test_http_failures_map_to_typed_errors[403-headers1-body1-AuthRevoked] PASSED
tests/test_github_rest.py::test_http_failures_map_to_typed_errors[404-headers2-body2-AuthRevoked] PASSED
tests/test_github_rest.py::test_http_failures_map_to_typed_errors[403-headers3-body3-RateLimited] PASSED
tests/test_github_rest.py::test_http_failures_map_to_typed_errors[403-headers4-body4-RateLimited] PASSED
tests/test_github_rest.py::test_http_failures_map_to_typed_errors[429-headers5-body5-RateLimited] PASSED
tests/test_github_rest.py::test_http_failures_map_to_typed_errors[502-headers6-body6-TransientOutage] PASSED
tests/test_github_rest.py::test_secondary_rate_limit_carries_retry_after PASSED
tests/test_github_rest.py::test_connection_refused_is_transient PASSED
tests/test_github_rest.py::test_checks_forbidden_degrades_instead_of_blocking PASSED
tests/test_github_rest.py::test_client_only_ever_sends_get PASSED
tests/test_github_rest.py::test_first_read_past_page_cap_reaches_the_tail PASSED
tests/test_github_rest.py::test_truncated_first_read_warns_then_backfills PASSED
tests/test_github_rest.py::test_steady_state_reads_page_one_and_the_tail_only PASSED
tests/test_github_rest.py::test_page_boundary_changes_between_polls[delete_then_append] PASSED
tests/test_github_rest.py::test_page_boundary_changes_between_polls[grow_two_pages] PASSED
tests/test_github_rest.py::test_deletion_during_a_walk_loses_nothing PASSED
tests/test_github_rest.py::test_fault_mid_walk_raises_then_retry_recovers[fault0-RateLimited] PASSED
tests/test_github_rest.py::test_fault_mid_walk_raises_then_retry_recovers[fault1-RateLimited] PASSED
tests/test_github_rest.py::test_fault_mid_walk_raises_then_retry_recovers[fault2-TransientOutage] PASSED
tests/test_github_rest.py::test_pending_review_is_never_a_stop_page PASSED
tests/test_github_rest.py::test_capped_check_runs_report_truncation PASSED
tests/test_inference_contracts.py::test_values_are_frozen PASSED
tests/test_inference_contracts.py::test_request_snapshots_its_context PASSED
tests/test_inference_contracts.py::test_request_rejects_invalid_fields[kw0] PASSED
tests/test_inference_contracts.py::test_request_rejects_invalid_fields[kw1] PASSED
tests/test_inference_contracts.py::test_request_rejects_invalid_fields[kw2] PASSED
tests/test_inference_contracts.py::test_request_rejects_invalid_fields[kw3] PASSED
tests/test_inference_contracts.py::test_request_rejects_invalid_fields[kw4] PASSED
tests/test_inference_contracts.py::test_request_rejects_invalid_fields[kw5] PASSED
tests/test_inference_contracts.py::test_capabilities_reject_invalid_fields[kw0] PASSED
tests/test_inference_contracts.py::test_capabilities_reject_invalid_fields[kw1] PASSED
tests/test_inference_contracts.py::test_capabilities_reject_invalid_fields[kw2] PASSED
tests/test_inference_contracts.py::test_capabilities_reject_invalid_fields[kw3] PASSED
tests/test_inference_contracts.py::test_request_carries_no_identity PASSED
tests/test_inference_contracts.py::test_completion_belongs_to_the_call_not_the_provider PASSED
tests/test_inference_contracts.py::test_output_is_deterministic_per_request PASSED
tests/test_inference_contracts.py::test_stream_completes_with_usage PASSED
tests/test_inference_contracts.py::test_max_tokens_truncates_with_length_finish PASSED
tests/test_inference_contracts.py::test_closed_stream_has_no_result PASSED
tests/test_inference_contracts.py::test_interleaved_calls_never_cross PASSED
tests/test_inference_contracts.py::test_concurrent_calls_keep_their_own_results PASSED
tests/test_inference_contracts.py::test_fault_at_chunk_index[0] PASSED
tests/test_inference_contracts.py::test_fault_at_chunk_index[1] PASSED
tests/test_inference_contracts.py::test_fault_at_chunk_index[3] PASSED
tests/test_inference_contracts.py::test_fault_at_chunk_index[8] PASSED
tests/test_inference_contracts.py::test_fault_on_first_attempt_only_and_counts PASSED
tests/test_inference_contracts.py::test_pre_dispatch_refusals_are_not_invocations PASSED
tests/test_inference_contracts.py::test_cancel_before_first_chunk PASSED
tests/test_inference_contracts.py::test_cancel_wakes_a_long_delay PASSED
tests/test_inference_contracts.py::test_cancel_beats_expired_deadline PASSED
tests/test_inference_contracts.py::test_expired_deadline_yields_no_chunks PASSED
tests/test_inference_contracts.py::test_delay_crossing_deadline_times_out_mid_stream PASSED
tests/test_inference_contracts.py::test_cancel_token_wait PASSED
tests/test_inference_contracts.py::test_error_classification[ProviderUnavailable-True-False] PASSED
tests/test_inference_contracts.py::test_error_classification[ModelCrashed-True-False] PASSED
tests/test_inference_contracts.py::test_error_classification[Timeout-True-False] PASSED
tests/test_inference_contracts.py::test_error_classification[RateLimited-True-False] PASSED
tests/test_inference_contracts.py::test_error_classification[Overloaded-True-True] PASSED
tests/test_inference_contracts.py::test_error_classification[StreamInterrupted-True-False] PASSED
tests/test_inference_contracts.py::test_error_classification[ContextConflict-True-True] PASSED
tests/test_inference_contracts.py::test_error_classification[CapabilityUnsupported-False-True] PASSED
tests/test_inference_contracts.py::test_error_classification[ContextTooLong-False-True] PASSED
tests/test_inference_contracts.py::test_error_classification[BudgetExceeded-False-True] PASSED
tests/test_inference_contracts.py::test_error_classification[EgressDenied-False-True] PASSED
tests/test_inference_contracts.py::test_error_classification[AuthFailed-False-False] PASSED
tests/test_inference_contracts.py::test_error_classification[InvalidRequest-False-False] PASSED
tests/test_inference_contracts.py::test_error_classification[ProviderCancelled-False-False] PASSED
tests/test_inference_contracts.py::test_error_classification[AttemptsExhausted-False-False] PASSED
tests/test_inference_contracts.py::test_errors_carry_retry_hints PASSED
tests/test_inference_contracts.py::test_health_is_scripted_and_counted PASSED
tests/test_inference_journal.py::test_ask_commits_one_message_settles_ledger_and_posts_once PASSED
tests/test_inference_journal.py::test_crash_point_recovers_to_one_answer_50_seeds[E1_before_reserve] PASSED
tests/test_inference_journal.py::test_crash_point_recovers_to_one_answer_50_seeds[E2_after_reserve] PASSED
tests/test_inference_journal.py::test_crash_point_recovers_to_one_answer_50_seeds[E3_after_provider] PASSED
tests/test_inference_journal.py::test_crash_point_recovers_to_one_answer_50_seeds[E4_after_commit] PASSED
tests/test_inference_journal.py::test_sigkill_at_crash_point_recovers_to_one_answer[E1_before_reserve] PASSED
tests/test_inference_journal.py::test_sigkill_at_crash_point_recovers_to_one_answer[E2_after_reserve] PASSED
tests/test_inference_journal.py::test_sigkill_at_crash_point_recovers_to_one_answer[E3_after_provider] PASSED
tests/test_inference_journal.py::test_sigkill_at_crash_point_recovers_to_one_answer[E4_after_commit] PASSED
tests/test_inference_journal.py::test_replay_of_done_call_short_circuits_without_provider PASSED
tests/test_inference_journal.py::test_duplicate_commit_of_one_reservation_writes_one_row PASSED
tests/test_inference_journal.py::test_concurrent_commits_of_two_attempts_keep_only_the_latest PASSED
tests/test_inference_journal.py::test_concurrent_commit_of_one_reservation_from_two_connections PASSED
tests/test_inference_journal.py::test_concurrent_calls_on_distinct_conversations_all_commit PASSED
tests/test_inference_journal.py::test_two_calls_on_one_conversation_conflict_then_rebase PASSED
tests/test_inference_journal.py::test_foreign_chunks_and_results_are_rejected_and_write_nothing PASSED
tests/test_inference_journal.py::test_stale_owner_cannot_commit_after_takeover PASSED
tests/test_inference_journal.py::test_released_owner_cannot_commit PASSED
tests/test_inference_journal.py::test_expired_owner_with_no_successor_cannot_commit PASSED
tests/test_inference_journal.py::test_same_generation_zombie_cannot_commit_a_settled_attempt PASSED
tests/test_inference_journal.py::test_append_is_idempotent_and_refuses_conflicts PASSED
tests/test_inference_journal.py::test_call_and_conversation_ownership_are_enforced PASSED
tests/test_inference_journal.py::test_attempt_cap_survives_crash_loop_restarts PASSED
tests/test_inference_journal.py::test_drain_cancel_mid_stream_keeps_partial_text_out_and_call_pending PASSED
tests/test_inference_journal.py::test_operator_cancel_discards_a_late_result_and_never_reruns PASSED
tests/test_inference_journal.py::test_interrupted_stream_commits_nothing PASSED
tests/test_inference_journal.py::test_pre_dispatch_refusal_consumes_no_attempt_or_budget PASSED
tests/test_inference_journal.py::test_reservation_is_durable_before_dispatch_and_lost_charges_in_full PASSED
tests/test_inference_journal.py::test_provider_swap_keeps_identity_and_resends_full_context PASSED
tests/test_inference_journal.py::test_effect_citing_an_uncommitted_call_is_refused PASSED
tests/test_inference_router.py::test_local_only_never_reaches_cloud_even_when_local_fails PASSED
tests/test_inference_router.py::test_egress_revocation_is_durable_and_stops_cloud PASSED
tests/test_inference_router.py::test_revocation_after_selection_is_caught_inside_the_reserve_txn PASSED
tests/test_inference_router.py::test_policy_refusal_never_becomes_cloud_fallback PASSED
tests/test_inference_router.py::test_capability_constraint_and_context_window_filtering PASSED
tests/test_inference_router.py::test_local_first_by_default_and_configured_preference PASSED
tests/test_inference_router.py::test_unhealthy_and_rate_limited_providers_are_filtered PASSED
tests/test_inference_router.py::test_retryable_failure_falls_back_and_records_degraded PASSED
tests/test_inference_router.py::test_cancellation_mid_stream_is_terminal_without_fallback PASSED
tests/test_inference_router.py::test_failure_after_dispatch_charges_and_refusal_before_dispatch_does_not PASSED
tests/test_inference_router.py::test_at_most_three_attempts_across_restart PASSED
tests/test_inference_router.py::test_auth_failure_is_terminal_until_config_changes PASSED
tests/test_inference_router.py::test_auth_block_keeps_provider_out_of_other_calls PASSED
tests/test_inference_router.py::test_budget_exhaustion_refuses_before_dispatch_and_resets PASSED
tests/test_inference_router.py::test_lost_and_outstanding_reservations_count_across_restart PASSED
tests/test_inference_router.py::test_concurrent_admissions_never_oversubscribe_the_final_reservation PASSED
tests/test_inference_router.py::test_untrusted_paid_cloud_needs_soft_grant_and_reports_overrun PASSED
tests/test_inference_router.py::test_trusted_bound_violation_is_hard_overrun_and_demotes PASSED
tests/test_inference_router.py::test_local_untrusted_runs_soft_without_a_grant PASSED
tests/test_inference_router.py::test_fifo_queue_capacity_and_slot_release PASSED
tests/test_inference_router.py::test_queue_timeout_and_cancel_leave_no_slot_or_waiter PASSED
tests/test_inference_router.py::test_free_slot_on_later_candidate_and_call_limits PASSED
tests/test_inference_router.py::test_router_releases_slots_and_tickets_on_every_exit PASSED
tests/test_inference_router.py::test_same_conversation_second_call_is_overloaded PASSED
tests/test_inference_router.py::test_routed_answer_keeps_identity_and_commits_once PASSED
tests/test_inference_router.py::test_provider_failure_never_completes_the_responsibility PASSED
tests/test_inference_router.py::test_one_thousand_randomized_calls_leak_nothing PASSED
tests/test_invariants.py::test_i1_identity_stable_across_restart PASSED
tests/test_invariants.py::test_i2_single_owner PASSED
tests/test_invariants.py::test_i3_checkpoint_before_effect PASSED
tests/test_invariants.py::test_i4_idempotent_effect PASSED
tests/test_invariants.py::test_i5_inbox_replay_and_dedup PASSED
tests/test_invariants.py::test_i6_authorization_durable PASSED
tests/test_invariants.py::test_i7_outcome_completeness PASSED
tests/test_lease_fencing.py::test_renew_after_takeover_raises_fenced_error PASSED
tests/test_lease_fencing.py::test_write_txn_keeps_original_exception PASSED
tests/test_lease_fencing.py::test_run_forever_exits_4_after_takeover PASSED
tests/test_lease_fencing.py::test_stale_owner_cannot_commit PASSED
tests/test_lease_fencing.py::test_stale_notification_rejected_then_recovered_once PASSED
tests/test_lifecycle.py::test_drain_keeps_lease_live_and_renewable PASSED
tests/test_lifecycle.py::test_release_fences_old_generation_without_successor PASSED
tests/test_lifecycle.py::test_expired_lease_refuses_renewal PASSED
tests/test_lifecycle.py::test_same_owner_reclaiming_draining_lease_bumps_generation PASSED
tests/test_lifecycle.py::test_stopped_supervisor_commits_nothing_more PASSED
tests/test_lifecycle.py::test_draining_refuses_new_work_before_it_persists PASSED
tests/test_lifecycle.py::test_cancel_leaves_event_pending_for_successor PASSED
tests/test_lifecycle.py::test_run_forever_drains_on_stop_and_releases PASSED
tests/test_lifecycle.py::test_v02_lease_table_migrates_in_place PASSED
tests/test_model_tracker.py::test_models_start_discovered_and_nothing_probes_before_demand PASSED
tests/test_model_tracker.py::test_transition_table_plans_and_wiring_are_validated PASSED
tests/test_model_tracker.py::test_concurrent_first_demand_loads_once PASSED
tests/test_model_tracker.py::test_ready_is_not_resident_until_a_token_streams PASSED
tests/test_model_tracker.py::test_unlisted_http_model_loads_only_through_the_endpoint_only_plan PASSED
tests/test_model_tracker.py::test_verify_ttl_reprobes_and_detects_external_eviction PASSED
tests/test_model_tracker.py::test_unavailable_http_endpoint_fails_after_three_plans_with_zero_requests PASSED
tests/test_model_tracker.py::test_cooldown_elapses_then_the_model_loads_again PASSED
tests/test_model_tracker.py::test_request_failures_never_suspect_the_model PASSED
tests/test_model_tracker.py::test_transport_failure_is_suspected_then_cleared_by_a_probe PASSED
tests/test_model_tracker.py::test_confirmed_crash_rematerializes_a_new_incarnation PASSED
tests/test_model_tracker.py::test_http_server_gone_is_suspected_then_crashed_then_failed PASSED
tests/test_model_tracker.py::test_crash_loop_stops_at_max_recoveries_and_success_resets PASSED
tests/test_model_tracker.py::test_cancel_while_waiting_for_a_load_costs_nothing_and_the_load_finishes PASSED
tests/test_model_tracker.py::test_operator_cancel_mid_call_is_terminal_and_leaves_the_model_healthy PASSED
tests/test_model_tracker.py::test_replacement_drains_the_active_call_and_serves_new_arrivals PASSED
tests/test_model_tracker.py::test_drain_deadline_cancels_the_old_call_and_reruns_it_on_the_successor PASSED
tests/test_model_tracker.py::test_arrival_during_replacement_without_wait_fails_over_without_an_attempt PASSED
tests/test_model_tracker.py::test_replacement_mid_load_is_judged_again_against_the_new_window PASSED
tests/test_model_tracker.py::test_replacement_that_turns_cloud_still_needs_the_egress_grant PASSED
tests/test_model_tracker.py::test_replacement_keeps_identity_and_conversation_and_resends_full_context PASSED
tests/test_model_tracker.py::test_restart_rebuilds_lifecycle_and_replays_without_provider_calls PASSED
tests/test_model_tracker.py::test_concurrent_calls_faults_and_replacements_leak_nothing PASSED
tests/test_openai_compat.py::test_sse_decoder_whole_stream PASSED
tests/test_openai_compat.py::test_sse_decoder_every_split_point_and_bytewise PASSED
tests/test_openai_compat.py::test_sse_decoder_rejects_bad_utf8_truncated_sequence_and_oversize PASSED
tests/test_openai_compat.py::test_sse_decoder_drops_event_cut_off_by_eof PASSED
tests/test_openai_compat.py::test_normal_completion_with_usage_and_wire_shape PASSED
tests/test_openai_compat.py::test_chunked_transfer_crlf_and_length_finish PASSED
tests/test_openai_compat.py::test_fragmented_writes_split_unicode_and_keepalives PASSED
tests/test_openai_compat.py::test_multiline_data_event_and_role_only_chunk PASSED
tests/test_openai_compat.py::test_two_simultaneous_calls_on_one_provider_keep_their_own_results PASSED
tests/test_openai_compat.py::test_empty_content_with_finish_is_a_valid_empty_answer PASSED
tests/test_openai_compat.py::test_missing_usage_yields_none_never_an_estimate PASSED
tests/test_openai_compat.py::test_malformed_usage_is_treated_as_missing[bad0] PASSED
tests/test_openai_compat.py::test_malformed_usage_is_treated_as_missing[bad1] PASSED
tests/test_openai_compat.py::test_malformed_usage_is_treated_as_missing[bad2] PASSED
tests/test_openai_compat.py::test_malformed_usage_is_treated_as_missing[bad3] PASSED
tests/test_openai_compat.py::test_malformed_usage_is_treated_as_missing[bad4] PASSED
tests/test_openai_compat.py::test_stream_usage_off_omits_stream_options PASSED
tests/test_openai_compat.py::test_malformed_or_truncated_streams_interrupt[reply0-before \\[DONE\\]] PASSED
tests/test_openai_compat.py::test_malformed_or_truncated_streams_interrupt[reply1-before \\[DONE\\]] PASSED
tests/test_openai_compat.py::test_malformed_or_truncated_streams_interrupt[reply2-before \\[DONE\\]] PASSED
tests/test_openai_compat.py::test_malformed_or_truncated_streams_interrupt[reply3-without a finish reason] PASSED
tests/test_openai_compat.py::test_malformed_or_truncated_streams_interrupt[reply4-undecodable] PASSED
tests/test_openai_compat.py::test_malformed_or_truncated_streams_interrupt[reply5-no choices] PASSED
tests/test_openai_compat.py::test_malformed_or_truncated_streams_interrupt[reply6-content_filter] PASSED
tests/test_openai_compat.py::test_malformed_or_truncated_streams_interrupt[reply7-failed] PASSED
tests/test_openai_compat.py::test_malformed_or_truncated_streams_interrupt[reply8-not an event] PASSED
tests/test_openai_compat.py::test_malformed_or_truncated_streams_interrupt[reply9-UTF 8] PASSED
tests/test_openai_compat.py::test_stream_error_object_and_tool_calls PASSED
tests/test_openai_compat.py::test_premature_disconnect_mid_stream_keeps_no_partial_result PASSED
tests/test_openai_compat.py::test_status_mapping[401-AuthFailed] PASSED
tests/test_openai_compat.py::test_status_mapping[403-AuthFailed] PASSED
tests/test_openai_compat.py::test_status_mapping[500-ProviderUnavailable] PASSED
tests/test_openai_compat.py::test_status_mapping[502-ProviderUnavailable] PASSED
tests/test_openai_compat.py::test_status_mapping[503-ProviderUnavailable] PASSED
tests/test_openai_compat.py::test_status_mapping[408-Timeout] PASSED
tests/test_openai_compat.py::test_status_mapping[400-InvalidRequest] PASSED
tests/test_openai_compat.py::test_status_mapping[404-InvalidRequest] PASSED
tests/test_openai_compat.py::test_rate_limit_retry_after[headers0-7.0] PASSED
tests/test_openai_compat.py::test_rate_limit_retry_after[headers1-1.5] PASSED
tests/test_openai_compat.py::test_rate_limit_retry_after[headers2-5.0] PASSED
tests/test_openai_compat.py::test_rate_limit_retry_after[headers3-300.0] PASSED
tests/test_openai_compat.py::test_rate_limit_retry_after_http_date PASSED
tests/test_openai_compat.py::test_redirect_is_refused_not_followed PASSED
tests/test_openai_compat.py::test_connection_refused_is_unavailable PASSED
tests/test_openai_compat.py::test_cancel_during_stalled_socket_read PASSED
tests/test_openai_compat.py::test_cancel_while_waiting_for_response_headers PASSED
tests/test_openai_compat.py::test_deadline_expires_during_stalled_read PASSED
tests/test_openai_compat.py::test_idle_stall_within_deadline_interrupts PASSED
tests/test_openai_compat.py::test_cancel_wins_over_expired_deadline_and_nothing_is_sent PASSED
tests/test_openai_compat.py::test_close_abandons_stream_and_closes_socket PASSED
tests/test_openai_compat.py::test_generate_refuses_before_dispatch PASSED
tests/test_openai_compat.py::test_locality_derives_from_validated_endpoint[http://127.0.0.1:30000/v1-Locality.LOCAL] PASSED
tests/test_openai_compat.py::test_locality_derives_from_validated_endpoint[http://127.8.9.10:1234/v1-Locality.LOCAL] PASSED
tests/test_openai_compat.py::test_locality_derives_from_validated_endpoint[http://[::1]:8080/v1-Locality.LOCAL] PASSED
tests/test_openai_compat.py::test_locality_derives_from_validated_endpoint[http://[::ffff:127.0.0.1]:8080/v1-Locality.LOCAL] PASSED
tests/test_openai_compat.py::test_locality_derives_from_validated_endpoint[https://127.0.0.1/v1-Locality.LOCAL] PASSED
tests/test_openai_compat.py::test_locality_derives_from_validated_endpoint[https://api.openai.com/v1-Locality.CLOUD] PASSED
tests/test_openai_compat.py::test_locality_derives_from_validated_endpoint[https://10.0.0.5:30000/v1-Locality.CLOUD] PASSED
tests/test_openai_compat.py::test_locality_derives_from_validated_endpoint[https://127.0.0.1.nip.io/v1-Locality.CLOUD] PASSED
tests/test_openai_compat.py::test_endpoint_rejections[http://localhost:1234/v1-localhost] PASSED
tests/test_openai_compat.py::test_endpoint_rejections[http://api.example.com/v1-requires https] PASSED
tests/test_openai_compat.py::test_endpoint_rejections[http://10.0.0.5:30000/v1-requires https] PASSED
tests/test_openai_compat.py::test_endpoint_rejections[http://192.168.1.2/v1-requires https] PASSED
tests/test_openai_compat.py::test_endpoint_rejections[https://user:pw@api.example.com/v1-credentials] PASSED
tests/test_openai_compat.py::test_endpoint_rejections[ftp://127.0.0.1/v1-scheme] PASSED
tests/test_openai_compat.py::test_endpoint_rejections[https://api.example.com/v1?key=x-query] PASSED
tests/test_openai_compat.py::test_locality_label_cannot_be_supplied PASSED
tests/test_openai_compat.py::test_key_is_sent_on_the_wire_and_redacted_everywhere_else PASSED
tests/test_openai_compat.py::test_config_fingerprint_tracks_key_endpoint_and_model PASSED
tests/test_openai_compat.py::test_health_ok_degraded_down PASSED
tests/test_openai_compat.py::test_router_commits_http_answer_and_settles_reported_usage PASSED
tests/test_openai_compat.py::test_missing_usage_through_router_charges_full_reservation PASSED
tests/test_openai_compat.py::test_failed_stream_commits_no_partial_text_and_falls_back PASSED
tests/test_openai_compat.py::test_failed_only_provider_leaves_call_pending_with_no_message PASSED
tests/test_openai_compat.py::test_router_falls_back_from_refused_local_provider PASSED
tests/test_openai_compat.py::test_cloud_http_provider_needs_durable_grant PASSED
tests/test_openai_compat.py::test_auth_failure_records_fingerprint_not_key PASSED
tests/test_openai_compat.py::test_rate_limit_from_http_opens_circuit PASSED
tests/test_openai_compat.py::test_crash_points_recover_with_http_provider[E1_before_reserve] PASSED
tests/test_openai_compat.py::test_crash_points_recover_with_http_provider[E2_after_reserve] PASSED
tests/test_openai_compat.py::test_crash_points_recover_with_http_provider[E3_after_provider] PASSED
tests/test_openai_compat.py::test_crash_points_recover_with_http_provider[E4_after_commit] PASSED
tests/test_recovery.py::test_a_crash_before_intent PASSED
tests/test_recovery.py::test_b_crash_after_intent_before_dispatch PASSED
tests/test_recovery.py::test_c_crash_after_remote_before_journal PASSED
tests/test_recovery.py::test_d_crash_after_journal_done PASSED
tests/test_recovery.py::test_e_two_outstanding_responsibilities PASSED
tests/test_recovery.py::test_f_two_concurrent_supervisors PASSED
tests/test_recovery.py::test_g_stale_worker_cannot_commit PASSED
tests/test_recovery.py::test_h_ambiguous_blocks_and_escalates PASSED
tests/test_recovery.py::test_i_model_replacement_keeps_identity PASSED
tests/test_recovery.py::test_delayed_remote_visibility PASSED
tests/test_recovery.py::test_stale_external_effect_rejected_by_destination PASSED
tests/test_recovery.py::test_authorization_revocation_survives_restart PASSED
tests/test_recovery.py::test_real_subprocess_kill_then_recover PASSED
tests/test_takeover.py::test_stale_owner_exits_and_successor_recovers_after_sigkill PASSED
316 passed
```

All 293 tests that predate the lifecycle tracker run unchanged. `uv run ruff check .` reports no findings.
