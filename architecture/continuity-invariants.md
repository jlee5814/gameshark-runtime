# GameShark continuity invariants

The properties the supervisor must hold for durable responsibility ownership. Each maps to a durable storage requirement and to the state machine transition it guards (see `agent-lifecycle.md`). Violating any one reintroduces duplicate consequential actions or lost ownership.

## Invariants

### I1. Identity stability across restart
The agent uuid is assigned once and reloaded, never regenerated.

- Durable requirement: identity record, write once, read on every start.
- Guards transition: cold → claimed.
- Discord anchor: identity bound to keychain material (`com.hnc.agekeys`, Observed), reloaded not recreated.
- Failure if violated: a restart creates a new owner, orphaning in flight responsibilities and breaking accountability.

### I2. Single owner at a time
At most one process holds a responsibility set, enforced by an ownership lease.

- Durable requirement: lease record with owner token and ttl, atomic acquire.
- Guards transition: cold → claimed, and crash → claimed on restart.
- Fencing: a takeover bumps the lease generation. Every durable write, including the companion notification insert, runs in one transaction guarded by the writer's generation and lease expiry, so a stale, released, or expired owner commits nothing, even before a successor acquires.
- Stale owner exit: a renewal that finds a newer generation raises FencedError and the worker exits with code 4. Retrying cannot succeed, and the stale process must not keep its instance lock.
- Rollback discipline: every write transaction rolls back at most once through `store.write_txn`, so a failed rollback never masks FencedError.
- Discord anchor: one long lived main process owns the module set; the commit record has a single writer.
- Failure if violated: two owners both act, duplicating every consequential effect.

### I3. Checkpoint before effect
Intent is committed durably before the external effect runs.

- Durable requirement: journal row status pending written and flushed before the effect call.
- Guards transition: running → effecting.
- Discord anchor: `existsSync(firstRunCompletePath)` guard precedes the once only task.
- Failure if violated: a crash after the effect leaves no record, so replay cannot tell the effect happened and will repeat it.

### I4. Idempotent effect application
Every consequential effect carries a stable idempotency key, and the destination either deduplicates by that key or exposes a probe that reports whether the effect landed.

- Durable requirement: the key is part of the journal row; the outcome row stores the external id.
- Guards transition: effecting → running.
- Discord anchor: the first run sentinel records outcome; update tasks carry `skip_host_download` / `skip_module_download` so a completed step is a no op on retry.
- Failure if violated: delivery at least once becomes duplicate consequence.

### I5. Deliver at least once, then dedup
No event or effect is dropped; duplicates are absorbed by I3 and I4 rather than prevented at the transport.

- Durable requirement: pending journal rows survive restart and are replayed on recovery.
- Guards transition: claimed → active (replay), and crash → claimed.
- Discord anchor: staged `PENDING_UPDATES` resume rather than restart.
- Failure if violated: a dropped effect silently loses a consequential action, the opposite failure from duplication and equally wrong.

### I6. Authorization never widens on recovery
An adapter's capability grant after restart equals its grant before, never more.

- Durable requirement: the capability grant is derived deterministically from the adapter declaration, not from mutable runtime state.
- Guards transition: claimed → active (reopening adapters).
- Discord anchor: the preload exposes a fixed channel set; recovery reuses the same boundary.
- Failure if violated: a crash becomes a privilege escalation path.

### I7. Outcome completeness
Every journal row with status done has a non null external id.

- Durable requirement: the outcome write includes the destination's returned id, or the probed id.
- Guards transition: effecting → running.
- Failure if violated: a row marked done without proof of the effect cannot be audited, undermining exactly once claims.

## Invariant to transition matrix

| Invariant | Durable transition it guards | Breaks what if violated |
|---|---|---|
| I1 identity stability | cold → claimed | accountability |
| I2 single owner | cold → claimed, crash → claimed | exactly once (two owners) |
| I3 checkpoint before effect | running → effecting | recoverability |
| I4 idempotent effect | effecting → running | exactly once (duplicate) |
| I5 at least once plus dedup | claimed → active, crash → claimed | completeness |
| I6 no auth widening | claimed → active | security boundary |
| I7 outcome completeness | effecting → running | auditability |

## Reading order for the invariants

I2 then I1 establish who owns. I3, I4, I7 make each consequential action exactly once. I5 makes nothing get lost. I6 makes recovery safe. Durable responsibility ownership is the conjunction of all seven; drop any one and the apex guarantee fails.

## Minimum durable writes per consequential action

Two: one pending before the effect (I3), one done after (I4, I7). This is the irreducible cost, and it is the same two write shape Discord uses for its first run sentinel generalized to a keyed journal. Anything cheaper cannot survive a crash placed between the effect and its record.
