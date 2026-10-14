# GitHub monitor: Stark's first persistent real world responsibility

Status: v0.2, implemented in `prototype/`. Read only, deterministic, no LLM.

## Goal

One prompt assigns a responsibility (`stark watch owner/repo#N`). From then on Stark
watches that pull request against real GitHub, survives process death, reboots, offline
periods, rate limits, and token revocation, and tells the user about meaningful changes
exactly once. Nobody has to prompt Stark again.

## Layers

```
 operator ──stark watch/resume/unwatch──▶ control_requests (no lease needed)
                                                │
 ┌──────────── stark run (one process per host, flock) ─────────────────────┐
 │ worker.run_forever      schedules only: renew lease, apply controls,     │
 │                         poll, sleep until the earliest due duty          │
 │ AgentSupervisor         identity (assign once), fenced lease, inbox,     │
 │                         effect journal, durable grants        [v0.1]     │
 │ ResponsibilityStore     monitor_pull_request rows drive polling          │
 │ GitHubMonitorAdapter    environment adapter: seen set, backoff, audit    │
 │   ├─ RestGitHubClient   GET only transport, typed errors, no sleeping    │
 │   ├─ github_events      raw JSON → stable logical events (pure)          │
 │   └─ classify           logical event → kind, relevance, reason (pure)   │
 │ Companion               notifications table, unread state, banner hook   │
 └──────────────────────────────────┬───────────────────────────────────────┘
                                    │ every write fenced by lease generation
                           runtime.db (SQLite, WAL, synchronous=FULL)
```

Separation holds as in v0.1: identity and ownership never touch GitHub; the adapter never
owns identity; the worker owns no durable truth; the client knows nothing of storage.

## One poll of one pull request

```
due? (next_poll_at <= now)            no → skip, report waiting
  │ yes
snapshot(repo, pr, stored ETags, head sha)
  ├─ RateLimited(t)    → backoff until now+t; notify once if t > 15 min
  ├─ TransientOutage   → fail_count+1, backoff 30 s × 2^(n-1), cap 15 min
  ├─ AuthRevoked       → inbox system event auth_revoked:{episode}
  │                        handler: status auth_revoked, responsibility blocked,
  │                        one notification; no polling until `stark resume`
  └─ Snapshot
       normalize → logical events in time order
       for each event not in monitor_seen:
           older than baseline_before?  → audit "baseline:", no notify
           else submit to durable inbox → classify → audit → notify effect
           mark seen (and last conclusion per check name) in one fenced txn
       pr_state event → notify, responsibility done
       offline gap over 1 h with new activity → one catch up notice
       persist ETags, head sha, viewer, last_ok_at, next_poll_at
```

## Why no event is lost or doubled

| Hazard | Mechanism |
|---|---|
| Unrelated id spaces (reviews vs comments) | novelty is set membership in `monitor_seen`, never id order |
| Crash after inbox submit, before mark seen | resubmit hits the inbox dedup; replay already handled it |
| Crash inside notification delivery | effect journal intent/outcome plus `op_key` dedup in `notifications` |
| Crash during backoff | `next_poll_at` and `fail_count` persist; restart keeps waiting |
| Lists longer than one page | Link pagination; ETags stored only for single page streams, because new items land on the last page |
| ETag 304 | the stream yields nothing; everything in it was seen when its ETag was stored, and ETags persist only after a fully handled poll |
| Responsibility row without monitor row | polling derives targets from responsibilities and creates missing state |
| Token refused | system event enters the inbox before any state change, so its handler completes on replay |
| Watch during an outage | baseline is a timestamp cut, not a first poll flag, so activity during the failure still arrives |

## Classification rules (first match wins)

| Event | Kind | Delivered |
|---|---|---|
| system condition | system | yes |
| pull request merged or closed | pr_state | yes, completes the duty |
| check failed, timed out, action required, startup failure | ci_failure | yes |
| check passed after a failure of the same check name | ci_recovered | yes |
| other check conclusions | ci | no |
| any activity by the token's own login | self | no |
| review changes requested | change_request | yes |
| review approved | new_review | yes |
| bot comment or bot plain review | bot | no |
| review with body | review_comment | yes |
| review without body but with inline comments | review_comment | yes, one note for all |
| inline comment inside a review | review_comment | no, rolled up |
| standalone inline comment | review_comment | yes |
| discussion comment | comment | yes |
| acknowledgement (lgtm, +1, thanks) | comment | no |

Every decision lands in `classification_audit` with its reason, delivered or not. All
classifier inputs (viewer login, previous check conclusion) travel in the event payload,
so a replay decides identically.

## State

Additive to v0.1. Older databases migrate in place.

```sql
monitor_state      + status, next_poll_at, fail_count, last_ok_at, last_error, etags,
                     head_sha, viewer, baseline_before, episode
monitor_seen       (agent_id, repo, pr, event_id)          -- novelty
monitor_checks     (agent_id, repo, pr, name, conclusion)  -- CI recovery detection
control_requests   (id, adapter, action, payload, status)  -- operator requests
notifications      + url, read_at
```

Responsibility states: `active` (polled), `blocked` (GitHub refused access; waits for
`resume`), `done` (merged, closed, or unwatched).

## Process model

- `stark run` holds an exclusive `flock` on `runtime.db.lock`. The kernel releases it on
  death, so a restarted daemon starts at once. A second daemon exits with code 3.
- The owner token is stable per host, so a restart reclaims its own live lease and keeps
  the generation. Another host still needs the lease to expire, which fences it.
- The lease TTL is three poll intervals (minimum 30 s); the loop wakes at least every
  third of the TTL to renew.
- SIGTERM or SIGINT drains and releases the lease. SIGKILL loses nothing.
- CLI commands append to `control_requests`. With no daemon, the CLI takes the lock and
  applies them itself; with a daemon, the next tick applies them.

To start at login on macOS, a `launchd` agent that runs
`uv run --project <repo>/prototype python <repo>/prototype/stark.py run --notify` with
`KeepAlive` works; the runtime installs nothing on its own.

## Authorization

- The client exposes `viewer` and `snapshot` only, and its single network primitive asserts
  GET. A test asserts no other method reaches the wire and no other method string exists
  in the module.
- Token: `GITHUB_TOKEN`, else `gh auth token`. Prefer a fine grained token with read only
  Pull requests, Checks, and Metadata on the watched repositories. The gh fallback token
  usually carries write scope; the client cannot use it, but the credential can.
- Local revocation (`github.read` grant) stops polling with responsibilities intact, as in
  v0.1. Remote revocation (401, 403, 404) blocks and notifies, as above.
- A token without checks access degrades: reviews and comments continue and status shows
  "CI unavailable to this token".

## Limitations

- Polling, not webhooks. Latency equals the interval plus GitHub's own eventual
  consistency. Webhooks need a public endpoint, which a local companion lacks.
- Legacy commit statuses (the statuses API, used by some external CI) are not read; only
  check runs.
- Check runs come from the current head commit; runs on superseded commits are ignored.
- Backoff has no jitter, so tests stay deterministic; one host polling a handful of pull
  requests does not need it.
- A 404 for a deleted repository and a 404 for lost access look the same; both block.
- Noise rules are fixed heuristics with an audit trail, not learned preferences. Bot
  comments (Codex, Copilot summaries, coverage bots) count as noise; bot review decisions
  still notify.
- Baseline compares GitHub timestamps to the local clock; skew of seconds can misfile an
  event that lands in the same second as the watch.
- If `github.read` is revoked locally while an inbox event is pending from a crash, the
  replay raises at start and the daemon refuses to start until the grant returns. The
  event stays pending, not dropped (v0.1 behavior).
- Desktop banners are best effort and may repeat after a crash; the feed is authoritative.
- Single host. The lease is a SQLite row, not a distributed lock service.
