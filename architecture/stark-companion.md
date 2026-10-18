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
| `retry_reply {conversation_id, seq, presence_gen}` | write | answer a failed reply again (Phase 4.1) |
| `ready_out {handoff_id, draft, presence_gen}` | write | source confirms its exit |
| `ack_in {handoff_id, presence_gen}` | write | target confirms its arrival |
| `jack {handoff_id, to: self\|home, presence_gen}` | write | Jack In to the caller, or Jack Out home from the holder |

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

Paired phones reach the same queue and table over pinned TLS on the LAN (`stark.py run
--lan`); pairing, device proofs, revocation, and the threat model are in
[stark-pocket-transport.md](stark-pocket-transport.md). A Unix surface may not name a
`pocket-` surface, and a device's surface always comes from its device row.

## Live replies (Phase 4.1, `gameshark/stark_chat.py`)

Stark's replies come from the OpenAI compatible server `stark.py run --inference-url`
names, through the v0.3 router, tracker, executor, and journal; nothing here is a second
inference path. `run` refuses to start without an endpoint unless `--fake-model` asks for
the demo model by name, and every fake reply begins `[fake model]`. The snapshot's `model`
field (`{provider, model, locality, fake}`) is runtime configuration: changing it changes
no identity, conversation, or duty. `--inference-extra` adds server fields such as
`chat_template_kwargs` (Qwen3 needs `enable_thinking: false`, or its thinking spends the
reply budget); it joins the provider's config fingerprint and cannot replace a field the
provider sets.

Persona. Every reply request opens with Stark's runtime owned persona, not the prompt a
conversation stored at seq 0; see [stark-persona.md](stark-persona.md).

Context. The reply call sends a window, not the whole conversation: the persona plus the
newest rows whose byte bound fits `--context-window` minus `--max-tokens` minus a
256 byte margin, starting on a user turn (`journal.fit_history`). The bound assumes a byte
level tokenizer, so it trims early rather than late. Selection, reservation, and every
replay compute the same window from the same rows.

Reply state, in `activity.reply` (absent once answered):

| State | Source | Meaning |
|---|---|---|
| `queued` | the head row is unanswered, no call is streaming | accepted |
| `thinking` | the executor streams this seq's call | the model is answering |
| `waiting` | `InferenceTask.on_retry`, memory only | a retryable refusal: `error` (ModelUnavailable, Timeout, ...) and `retry_at` |
| `failed` | durable: the seq's newest reply event is acked and the head is unanswered | terminal: `error` is the duty's checkpoint |

Nothing ever writes text on Stark's behalf. A failed reply stays a user row without an
answer, the duty `chat:stark:main` turns `blocked`, and the surfaces show the reason.
`retry_reply` (presence fenced, like `send`) starts event `ev-reply-stark:main-<seq>-r<n>`
and call `call:stark:main:<seq>:r<n>`; a resent retry, before or after its answer lands,
finds the same `r<n>` and starts nothing. A new message supersedes a failed reply, as it
supersedes any unanswered one. A model that is down waits rather than fails: the tracker
refuses before dispatch, which costs no attempt, and re-probes after `--model-cooldown`
(10 s), so a restarted server is back within seconds. A call that dispatches and times out
counts an attempt; the third ends the reply as `failed` (AttemptsExhausted).

Durability is the v0.3 journal's. The user row commits before the reply event, the call is
one id per seq and retry, and the assistant row commits only through the journal, so a
runtime restart, a SIGKILL mid stream, or a surface that drops and resyncs all end with one
reply. On a physical iPhone, by hand, a phone disconnected mid reply saw the answer once
(no SIGKILL mid stream ran); see
[ios/StarkPocket/README.md](../ios/StarkPocket/README.md). Tests:
`prototype/tests/test_live_inference.py` (loopback SSE server: Mac and phone, multi turn, resent ids, timeout and retry, model down, phone drop, Jack In and Out while a
reply runs) and `test_gateway_daemon.py` (SIGKILL during an HTTP stream).

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

## Stark Pocket (`ios/StarkPocket`)

SwiftUI iPhone app on StarkKit: the same sprite atlas and animation rules as StarkMac, the
conversation, connection and presence state, pairing from a `starkpocket://pair` link, and
Jack In / Jack Out through the `jack` command. It stores only its pairing (Keychain) and
presentation facts. StarkMac names the device holding Stark and offers Jack In Here.
`ios/scripts/pocket-demo.sh` runs the full Mac → simulator → Mac handoff with faults; see
[ios/StarkPocket/README.md](../ios/StarkPocket/README.md). A physical iPhone passed the
Phase 3.3 cellular and Tailscale acceptance and the Phase 4.1 live inference acceptance by
hand; that README records what was shown and what remains unproven on hardware. Recall
across Jack In and a restart came from the conversation history window, not from long
term memory. The Phase 4.2 saved memory acceptance also passed by hand on a physical
iPhone; see [stark-memory.md](stark-memory.md#phase-42-acceptance-physical-iphone-2026-10-17).
The Phase 4.3A persona acceptance passed by hand on StarkMac and a physical iPhone; see
[stark-persona.md](stark-persona.md#phase-43a-acceptance-physical-iphone-and-starkmac-2026-10-18).

## Not yet built

Bonjour, voice input (voice output is built: [stark-voice-output.md](stark-voice-output.md)), system
notifications, automatic long term memory (Stark keeps only facts
the user saves; see [stark-memory.md](stark-memory.md)), a user cancel for a reply in
flight.
