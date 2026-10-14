# GameShark agent and adapter lifecycle

Generalized from Discord's module lifecycle (discover, load, initialize, update, terminate, recover; see `../research/discord/deep-analysis.md`). GameShark adds durable responsibility ownership on top.

## Two lifecycles, different lifetimes

1. **Agent** (long lived, like Discord's signed shell). An agent has a durable identity and owns one or more standing responsibilities. It outlives processes and adapter versions.
2. **Adapter** (swappable worker, like a Discord module). An adapter connects one agent to one external surface (a game, a GitHub PR, a chat). Adapters init and tear down freely; they never own identity.

## Agent lifecycle states

```
                 load identity record
     [ cold ] ─────────────────────────▶ [ claimed ]
        ▲                                     │
        │ process exit                        │ open adapters, replay journal
        │                                     ▼
   [ draining ] ◀───── stop ─────────── [ active ]
        ▲                                     │
        │                                     │ checkpoint ready
        │                                     ▼
        └────────── crash / restart ──── [ running ]
                                              │
                           consequential effect requested
                                              ▼
                                       [ effecting ]
                                              │ journal outcome
                                              ▼
                                        [ running ]
```

### State definitions

- **cold**: no identity loaded. The only durable fact is the identity record on disk.
- **claimed**: identity loaded, ownership lease acquired, adapters not yet open. The agent is the accountable owner but not yet acting.
- **active**: adapters opening or open, journal replayed, no in flight effect.
- **running**: steady state, watching events, holding responsibilities.
- **effecting**: a consequential external action is mid flight (a PR comment, a game move). This state exists only between journal intent and journal outcome.
- **draining**: graceful stop, adapters tearing down, lease released.

## Adapter lifecycle states

```
[ unloaded ] ──init──▶ [ connecting ] ──ready──▶ [ connected ]
     ▲                       │                        │
     │                       │ fail/backoff           │ disconnect
     │                       ▼                        ▼
     └────── teardown ── [ reconnecting ] ◀──────────┘
```

Reconnect uses backoff, mirroring Discord's `retryInSeconds = Math.min(...)` observation. A reconnect resumes the session; it never changes the owning agent identity.

## Transitions and durable storage

A transition marked durable must commit to storage **before** the transition is announced to any external party. This is the checkpoint before effect ordering.

| Transition | Durable storage required? | What must be committed | Why |
|---|---|---|---|
| cold → claimed | Yes | identity record read; ownership lease write | Establish single accountable owner; prevent two processes owning the same responsibility |
| claimed → active | Yes (read) | replay the effect journal | Recovery must know which effects already fired |
| active → running | No | in memory only | No external consequence |
| running → effecting | **Yes** | journal the effect intent with an idempotency key, status pending | A crash after the external call but before outcome must be recoverable as maybe done |
| effecting → running | **Yes** | journal the effect outcome, status done, with the external result id | Marks the effect complete so replay suppresses the duplicate |
| running → checkpoint (task step) | **Yes** | step completion flag per task | Resumed task continues from last step, like Discord `skip_module_download` |
| any → draining | Yes | lease release; flush journal | Allow a clean successor to claim ownership |
| crash → claimed (restart) | Yes (read) | reacquire lease; replay journal | Deterministic resume as a function of durable state |
| adapter connected → reconnecting | No | session resume token kept in memory or cache | Session continuity, not responsibility state |

### The critical ordering

The only transition that can duplicate a consequential action is running → effecting → running. GameShark enforces:

1. Write intent to the journal with an idempotency key. Commit. (running → effecting)
2. Perform the external effect.
3. Write outcome to the journal. Commit. (effecting → running)

A crash between steps 1 and 3 leaves a pending journal row. On restart the supervisor finds it, queries the external surface by idempotency key to learn whether the effect landed, and either completes the journal (effect landed) or retries once (effect did not land). This converts at least once delivery into exactly once consequence.

## Mapping back to Discord

- claimed ≈ Discord reading `installed.json` to learn what it owns.
- running → effecting → running ≈ Discord's `existsSync(firstRunCompletePath)` then task then `writeFileSync` sentinel, generalized from one boolean to a keyed journal.
- crash → claimed ≈ Discord restart reloading the committed module set.
- adapter reconnect ≈ Discord updater backoff retry, applied to a live session instead of a download.
