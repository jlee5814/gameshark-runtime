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
uv run pytest -v                 # all 154 tests (v0.1 + v0.2 + lease fencing + draining + expiry + v0.3 contracts)
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
144 passed
```

All 95 tests that predate the inference contracts run unchanged. `uv run ruff check .` reports no findings.
