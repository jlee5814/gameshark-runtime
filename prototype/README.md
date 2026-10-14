# GameShark Runtime v0.1 (prototype)

A clean room, deterministic prototype that proves an agent named Stark can lose his
process, restart from durable SQLite records, recover his responsibilities, and resume
cross environment work without silently losing commitments or duplicating externally
observable effects. No LLM, no real GitHub, no game client.

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
CREATE TABLE lease (agent_id TEXT PRIMARY KEY, owner_token TEXT, generation INTEGER, expires_at REAL);
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
running --stop--> draining ; crash --> (discard) --> cold (restart)
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
- Determinism by design: no real network, no clocks beyond an injectable one, no LLM.
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
uv run pytest -v                 # all 28 tests (v0.1 + v0.2)
uv run python demo.py            # v0.1 narrated Stark run with a real SIGKILL
uv run python demo_v2.py         # v0.2 PR monitoring across a real SIGKILL and a revoke
uv run python companion.py <runtime_db>   # render the companion feed for a run

# v0.2 critical tests
uv run pytest -v tests/test_github_monitor.py::test_real_subprocess_kill_mid_monitor_then_recover
uv run pytest -v tests/test_github_monitor.py::test_revoke_github_read_stops_monitoring_safely

# individual critical tests
uv run pytest -v tests/test_recovery.py::test_real_subprocess_kill_then_recover
uv run pytest -v tests/test_recovery.py::test_c_crash_after_remote_before_journal
uv run pytest -v tests/test_recovery.py::test_h_ambiguous_blocks_and_escalates
uv run pytest -v tests/test_recovery.py::test_g_stale_worker_cannot_commit
uv run pytest -v tests/test_recovery.py::test_stale_external_effect_rejected_by_destination
uv run pytest -v tests/test_recovery.py::test_authorization_revocation_survives_restart
```

## v0.2: first real responsibility (GitHub PR monitoring)

Stark accepts a durable `monitor_pull_request` responsibility, polls authorized pull
requests read only, classifies activity deterministically, and delivers relevant updates
to a local companion feed exactly once per logical event, surviving process death. All
changes are additive; the v0.1 supervisor is unchanged and every v0.1 test still passes.

GitHub source: a deterministic scripted transport drives tests and the demo (no
credentials). A real read only client (`RestGitHubClient`, token from `GITHUB_TOKEN`,
ETag conditional reads, pagination, rate limit and outage backoff) exists for live opt
in. The client class exposes no write method, so autonomous post, merge, or code change
is impossible by construction.

### Monitor flow

```
poll() ── list_events(cursor) ──▶ for each new logical event:
    submit_event(gh:repo:pr:id)  ──▶ durable inbox (dedup by event_id)
        on_event ──▶ classify ──▶ audit row (every decision)
                     if relevant ──▶ emit notification effect (journal: intent, outcome)
                                        companion feed (dedup by op_key)
    advance monitor_state.cursor   (fenced, only after the event is durably handled)
```

Ordering rule: the cursor advances only after an event is durably handled, so a crash
never skips an event; the inbox and the companion both deduplicate, so a re poll never
delivers twice.

### Classification (deterministic, auditable)

| Raw event | Kind | Delivered? | Reason |
|---|---|---|---|
| review approved | new_review | yes | reviewer approved the pull request |
| review changes_requested | change_request | yes | reviewer requested changes |
| review commented | comment | no | review comment without a state change |
| issue comment | comment | no | ordinary discussion comment |
| check_run failure/timed_out/cancelled | ci | yes | CI concluded <state> |
| check_run success | ci | no | CI concluded success |

Every decision, delivered or not, is written to `classification_audit`.

### New schema (additive)

```sql
CREATE TABLE monitor_state (agent_id TEXT, repo TEXT, pr INTEGER, etag TEXT,
    last_cursor TEXT, scope TEXT, updated REAL, PRIMARY KEY (agent_id, repo, pr));
CREATE TABLE notifications (op_key TEXT PRIMARY KEY, repo TEXT, pr INTEGER, kind TEXT,
    event_id TEXT, summary TEXT, reason TEXT, created REAL);
CREATE TABLE classification_audit (agent_id TEXT, repo TEXT, pr INTEGER, event_id TEXT,
    kind TEXT, relevant INTEGER, reason TEXT, created REAL,
    PRIMARY KEY (agent_id, repo, pr, event_id));
```

### Acceptance steps → coverage

| Brief step | Where |
|---|---|
| assign PR monitoring responsibility | `test_assign_then_quiet_poll_delivers_nothing` |
| monitoring continues without extra prompts | `poll()` driven by the runtime, not a human |
| introduce a new review event | `test_new_review_delivers_and_dedups_on_repoll` |
| identify and communicate the update | same test, companion feed `new_review` |
| kill during processing | `test_real_subprocess_kill_mid_monitor_then_recover` (SIGKILL) |
| restart | same test, fresh supervisor on same db |
| same uuid, responsibility, cursor recovered | same test asserts all three |
| no missed events, no duplicate notifications | subprocess + `test_crash_mid_delivery...` + dedup test |
| revoke permissions, monitoring stops safely | `test_revoke_github_read_stops_monitoring_safely` |

### Guarantees against a real GitHub account

| Property | Holds against real GitHub? | Basis / assumption |
|---|---|---|
| Read only, no autonomous writes | Yes | `RestGitHubClient` has no write methods |
| No duplicate user facing notification | Yes, under the assumptions | dedup by stable logical event id in the inbox and by op_key in the feed |
| Cursor resume after restart | Yes | durable `monitor_state`, advanced only after durable handling |
| Conditional reads save quota | Yes | ETag `If-None-Match`; pagination via `Link` |
| No missed events | Under the at least once polling assumption | relies on logical event id stability and on events being visible to the reads used |
| Timeliness | Not guaranteed | polling cadence, GitHub eventual consistency, and rate limits bound latency |

What stays unproven for real GitHub: logical event id stability across API responses, and
that the three read endpoints surface every meaningful event type. Those are assumptions
the deterministic transport encodes; a live soak test would be needed to validate them.

### v0.2 restrictions honored

Read only; no autonomous post, merge, or code change; no real MapleStory client; no LLM;
durable runtime state stays authoritative (the cursor and journal, not conversation
history); the supervisor was extended, not redesigned.

## Actual test results

```
platform darwin -- Python 3.13.9, pytest-9.1.1, pluggy-1.6.0
collected 28 items

tests/test_github_monitor.py::test_assign_then_quiet_poll_delivers_nothing PASSED
tests/test_github_monitor.py::test_new_review_delivers_and_dedups_on_repoll PASSED
tests/test_github_monitor.py::test_classification_kinds_and_relevance PASSED
tests/test_github_monitor.py::test_pagination_rate_limit_and_outage_resume PASSED
tests/test_github_monitor.py::test_duplicate_inbox_delivery_absorbed PASSED
tests/test_github_monitor.py::test_revoke_github_read_stops_monitoring_safely PASSED
tests/test_github_monitor.py::test_crash_mid_delivery_recovers_without_duplicate PASSED
tests/test_github_monitor.py::test_real_subprocess_kill_mid_monitor_then_recover PASSED
```

Earlier v0.1 block retained below for reference.

```
platform darwin -- Python 3.13.9, pytest-9.1.1, pluggy-1.6.0
collected 20 items

tests/test_invariants.py::test_i1_identity_stable_across_restart PASSED
tests/test_invariants.py::test_i2_single_owner PASSED
tests/test_invariants.py::test_i3_checkpoint_before_effect PASSED
tests/test_invariants.py::test_i4_idempotent_effect PASSED
tests/test_invariants.py::test_i5_inbox_replay_and_dedup PASSED
tests/test_invariants.py::test_i6_authorization_durable PASSED
tests/test_invariants.py::test_i7_outcome_completeness PASSED
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

20 passed in 0.17s
```
