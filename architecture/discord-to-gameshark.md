# Discord to GameShark translation

How each observed Discord mechanism maps to a GameShark persistent agent requirement. Confidence tags carry over from `research/discord/deep-analysis.md`. GameShark code is clean room; Discord informs the problem shape only.

## Translation table

| Discord mechanism (evidence, confidence) | Problem it solves | Generalizable? | GameShark equivalent | Extra guarantee GameShark needs |
|---|---|---|---|---|
| Signed shell plus mutable module host (`installed.json`, `moduleInstallPath`, Observed) | Ship stable trusted core, update feature code often | Yes | Persistent supervisor plus hot swappable adapters | Supervisor identity must outlive every adapter and never be replaced by an adapter update |
| Stage then relaunch swap (`PENDING_UPDATES`, `quitAndInstall`, `app.relaunch`; mechanism Observed, atomicity Inferred) | Upgrade without partial state corruption | Yes | Stage adapter version, commit one record, restart worker | Commit must be atomic rewrite; a torn commit must fail closed to the previous version. GameShark makes atomicity a requirement rather than assuming Discord proves it |
| Single commit record (`installed.json`, Observed) | Make restart a deterministic reload | Yes, strongly | One durable manifest per responsibility set | Manifest write is the only authority; memory is a cache of it |
| First run sentinel (`firstRunCompletePath`, Observed) | Run a once only task exactly once across restarts | Yes, strongly | Per effect idempotency key journaled around each consequential action | Journal the intent before the effect, mark outcome after; recovery reads this to avoid duplicates |
| Update retry with backoff and skip flags (`retryInSeconds`, `skip_host_download`, Observed) | Recover from network or partial failure without redoing completed work | Yes | Resumable task steps with per step completion flags | Each step idempotent, so a retry of a completed step is a no op |
| Central main bus to fixed preload channels (`splashScreenPreload.js`, Observed) | Route events and enforce a boundary between trusted host and untrusted renderer | Yes | Supervisor event router plus per adapter capability contract | Adapter may only touch channels its authorization grants; recovery never widens the grant |
| Crash observation and relaunch (`render-process-gone`, Observed) | Detect worker death, report, restart | Yes | Supervisor watchdog over adapter workers | Restart must resume from last checkpoint, never from scratch for in flight work |
| Gateway resume and presence (Inferred, client absent) | Reestablish session and user presence after interruption | Partially | Adapter reconnect with session resume token | Session continuity is distinct from identity; resume must not mint a new identity |
| Identity in keychain (`com.hnc.agekeys`, Observed entitlement) | Stable account identity independent of process or socket | Yes | Durable agent identity record, secret material in OS keychain | Identity is assigned once and reloaded, never regenerated on restart |

## Concept taxonomy: five distinct kinds of persistence

These are often conflated. GameShark must keep them separate because they need different storage and different guarantees.

### Process persistence
The OS process keeps running. Discord analogue: the single long lived main process. Discord provides this weakly and does not depend on it, because restart recovery is deterministic. **GameShark must not depend on it either.** Treat the process as disposable.

### Session continuity
A transport or server side session resumes with its prior state, usually through a resume token and a heartbeat. Discord analogue: the gateway resume (Inferred, client absent). Provided by Discord at the client layer, not the bootstrapper. **GameShark adapters own this per connection.** A session is attached to an identity but is not the identity.

### Identity persistence
A stable actor identity that survives process death and socket loss, independent of both. Discord analogue: the account identity bound to keychain material (`com.hnc.agekeys`, Observed), reloaded rather than regenerated. **GameShark must add an explicit identity record** so an agent named Stark is the same accountable actor before and after a restart. Discord ties identity to a token; GameShark assigns a durable agent id and loads secrets from the keychain.

### Task persistence
A specific in flight unit of work survives restart through a checkpoint. Discord analogue: staged `PENDING_UPDATES` and `pending_requests` plus per task skip flags (Observed). **GameShark checkpoints each task step** so a resumed run continues from the last committed step.

### Durable responsibility ownership
An agent remains the accountable owner of a standing responsibility across every interruption, and no consequential action fires twice. This is strictly more than task persistence: a task completes, a responsibility is ongoing. Discord does **not** provide this directly; its closest primitive is the once only first run sentinel. **This is GameShark's actual target.** It requires identity persistence plus task persistence plus an idempotency guard on every consequential effect, bound to the owning identity.

### Why the distinctions matter

A system can have process persistence and still duplicate actions on restart if it lacks task persistence. It can have session continuity and still lose responsibility ownership if identity is regenerated. GameShark needs all five layered, with durable responsibility ownership as the apex guarantee. The lower four are necessary, none is sufficient alone.

## Mapping to deliverables

- Lifecycle of agents and adapters: `agent-lifecycle.md`.
- The minimal supervisor realizing the right column: `agent-supervisor.md`.
- The invariants that make durable responsibility ownership hold: `continuity-invariants.md`.
