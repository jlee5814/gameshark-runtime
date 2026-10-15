# Stark companion: presence, Jack In, and thin surfaces

Evidence behind these choices: [research/companions/analysis.md](../research/companions/analysis.md).

## Model

The runtime owns identity, duties, and conversations. A surface (the Mac panel, a paired
iPhone) is a view. Presence names the one surface acting as Stark's body, under a
monotonic generation. Jack In and Jack Out move presence; the agent never moves, and the
runtime stays on the Mac because stock iOS suspends background apps and runs no daemons.

## Invariants

- P1 single active presence: the `presence` row names exactly one surface; at most one
  handoff is open.
- P2 stale surfaces act on nothing: every surface write carries its presence generation and
  `guard_surface_in_txn` rejects a stale one inside the same transaction as the owner lease
  guard (I2 applied to surfaces).
- P3 every handoff resolves to one committed generation: deadlines persist as absolute
  times, so a runtime killed mid jack resolves identically after restart through `tick()`.
- Surface writes dedup by `client_msg_id` (I4); a dedup hit wins over the presence check,
  so a message that landed before a jack stays landed. A resend must match the original
  conversation and text; a reused id with other text is rejected. Reconnect resync replays rows after
  `last_seq` (I5).

## Jack state machine (`gameshark/presence.py`)

```
preparing --ready_out | prepare deadline (3 s)--> committed  gen+1, surface = target
preparing --target lost--------------------------> aborted    presence unchanged
committed --ack_in-------------------------------> acked
committed --ack deadline (10 s) | target lost----> reverted   gen+1, surface = home,
                                                              or source when home failed
```

`ready_out` names the calling surface and generation; only the jack's source at the
generation the jack started under may commit, checked in the commit transaction, and its
replay after commit returns the same handoff. The prepare deadline is a separate trusted
path driven only by `tick()`. An `ack_in` at or past its deadline reverts in its own
transaction and is rejected, so a late ack never lands even before `tick()` runs.

A silent source commits without its draft rather than stranding a live target. Crash
points `P1_after_prepare` and `P2_after_commit` are proven by real SIGKILL in
`tests/test_presence.py`.

## Surface writes (`gameshark/conversation.py`)

User messages land in the inference journal's `messages` table, so they are ordinary
context for the next model call; assistant rows still commit only through the journal.

## Surface gateway (`gameshark/gateway.py`, `gameshark/surface_api.py`)

`stark.py run` serves surfaces on `gateway/stark.sock` beside the database. Socket threads only
accept, authenticate, and frame; they never touch SQLite. They queue requests and set the
supervisor's wake event, and the coordinator thread (the one that owns the connection)
answers them between lease renewal, controls, polls, and inference pumps. Provider calls
run on the inference executor, so a slow model never delays a gateway reply: during a
gated reply the worst ping measured 0.3 ms.

Authentication, three layers:

1. The socket has mode 0600 in a private directory: created 0700 when missing, refused
   when it exists open to others. The runtime never chmods a directory it did not create.
   Socket and token paths follow one rule in Python and Swift: `--socket` or
   `STARK_SOCKET`, else `gateway/stark.sock` beside the database; `--token` or
   `STARK_TOKEN`, else `gateway.token` beside the socket.
2. The kernel's peer uid (`LOCAL_PEERCRED`, `SO_PEERCRED` on Linux) must equal ours.
3. The client answers a fresh 32 byte nonce with
   `HMAC_SHA256(token, "stark-gateway" 0 nonce 0 surface 0 proto)`, where the token is
   `gateway.token` (32 random bytes, mode 0600, created once). Python and Swift share a
   known answer vector in their tests.

Framing: a 4 byte big endian length, then UTF8 JSON, at most 64 KiB. Oversized or
malformed input ends the session. The challenge lists supported versions; a hello naming
another gets `unsupported_version`.

Protocol v1 is a closed table of typed handlers. Nothing maps a frame to code, a module,
or a process, and a test walks the AST of both modules to forbid shell and import
machinery. Surfaces reach only `stark:main`.

| Command | Kind | Effect |
|---|---|---|
| `ping` | read | liveness |
| `snapshot` | read | identity, presence, open handoff, responsibilities, head seq, activity projection |
| `sync {conversation_id, after_seq}` | read | rows after seq, 500 per page |
| `send {client_msg_id, conversation_id, text, presence_gen}` | write | `SurfaceConversation.submit`, then the reply event |
| `ready_out {handoff_id, draft, presence_gen}` | write | source confirms its exit |
| `ack_in {handoff_id, presence_gen}` | write | target confirms its arrival |

Pushes (`event` frames with a monotonic `rev`): `presence`, `responsibilities`,
`messages` (rows after the last seq that session was sent), then `activity`, in that
order, so a surface never shows Stark idle before his reply arrives. Delivery is at least
once; the surface merges by seq.

Invariants added:

- G1 a session's surface is bound at hello. No frame field can name another surface.
- G2 every write carries `presence_gen` and its presence guard runs in the same write
  transaction as the lease guard (P2 at the wire). Errors are typed: `stale_presence`,
  `handoff_conflict`, `invalid`, `draining`, `fenced`.
- G3 a surface holds no agent state. StarkMac keeps a seq keyed render cache rebuilt from
  `sync` on every session; only UI facts (panel position, a viewed cursor) persist locally.
- G4 the companion reply is one event per user seq. `poll()` resubmits the event for an
  unanswered head row, so a crash between the user write and the event insert heals on
  the next tick; a user write during a call makes the journal refuse that commit
  (ContextConflict) and the newer seq answers the full context.

`stark.py jack <surface>` moves presence through a control request. A silent source
commits after the prepare deadline; a target that never acks reverts home under a newer
generation. The acceptance demo uses this to show a live app turn ghost and come back.

## Activity projection (`gameshark/activity.py`)

Stark shows what his runtime is doing now. `ActivityTracker` is in memory and read only:
it holds no connection, writes nothing durable, and an AST test forbids it from reaching
SQLite. A restart starts it empty, and replayed events and the next poll rebuild it.

Two live sources. Work spans: an adapter wraps non inference execution of a
responsibility in `ctx.activity.work(rid, kind)` (the GitHub monitor wraps each due poll
and each event it handles). Spans form a multiset closed in `finally`, so concurrent
duties keep Stark working until the last ends, on success, failure, cancellation, or lease
loss alike. Inference: read from the executor's in flight tasks, which it drops on
commit, failure, abort, and drain, so nothing drifts from the real call set. Responsibility
rows cannot serve: a PR watch stays `active` for days between polls.

Wire (v1, additive): `{state, boot, owner_generation, inference: [{call_id,
conversation_id, stage}], work: [{responsibility_id, kind, count}], blocked: [rid],
working, call_id}`. `working` and `call_id` keep their v1 meaning, the companion reply in
flight. `boot` is minted per runtime instance. A span's start pushes at once, because the
work may hold the coordinator (a GitHub GET runs on it); its end waits for the next pump,
after any new rows, keeping the rows before activity rule.

StarkMac rebuilds activity from each session's snapshot, ignores pushes before it or from
another `boot`, and clears it on disconnect. Mapping, first match wins:

| Animation | Condition |
|---|---|
| gallery, walk, blink | override, drag, idle beat (unchanged, above the base) |
| sleep | no live session |
| idle, dimmed | another surface holds presence |
| error | a responsibility is blocked |
| working | `work` non empty, or within a 1.2 s view only hold |
| thinking | `inference` non empty |
| listening | chat field focused (local) |
| notify | unviewed reply (local) |
| sleep | 10 min quiet |
| idle | otherwise |

## StarkMac (`mac/StarkMac`)

SwiftPM package, macOS 14, Swift 6 language mode. `StarkKit` holds framing, auth, the
POSIX socket transport, the reconnecting `GatewayClient` (jittered backoff 0.25 s to 5 s,
outbox resent under its original `client_msg_id`, identity change wipes the cache), the
`ConversationMirror`, and the pure `AvatarState` derivation. `StarkMac` is a borderless
nonactivating `NSPanel` at floating level on every Space, a status item (Click Through,
Show Chat, Reconnect, Save Snapshot, Quit), and an original vector avatar with idle,
listening, working, notification, ghost, and disconnected states. Public APIs only. See
[mac/StarkMac/README.md](../mac/StarkMac/README.md).

Stark's sprite animation is view state. `StarkAnimation.base(for:)` reads ClientState
(connection, presence, the activity projection, blocked duties, unviewed replies) and the
chat field's focus; nothing in the animation path can send a frame to the runtime. Position and
scale live in the app's UserDefaults. A visual change can therefore never alter identity,
ownership, presence, or inference (G3).

## Not yet built

Pinned TLS gateway, device pairing, Bonjour, StarkPhone, voice, system notifications.
