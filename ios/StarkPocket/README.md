# Stark Pocket

Stark's iPhone surface. It shows the same Stark the Mac runs: identity, duties, and the
conversation live in the GameShark runtime on the Mac; the phone is a view that can hold
Stark's presence. Jack In pulls Stark onto the phone (StarkMac dims and turns read only);
Jack Out sends him home. Transport, pairing, and the threat model:
[architecture/stark-pocket-transport.md](../../architecture/stark-pocket-transport.md).

## What it stores

Only the pairing record and the device key, in the Keychain (this device only, after first
unlock; the key lives in the Secure Enclave when the hardware has one), plus presentation
facts in UserDefaults (the last reply viewed, voice settings, and the last reply handled by
speech). Conversation rows are rebuilt from the runtime on every connection.

Stark reads his new replies aloud while he is on the phone: mute, stop, and replay sit in
the toolbar and beside each bubble, and Settings picks the voice. Design, rules, and the
physical checklist: [architecture/stark-voice-output.md](../../architecture/stark-voice-output.md).
The checklist passed by hand on 2026-10-18; see Voice output (Phase 4.3B).

## Build and test

Requires Xcode with an iOS 17 or later simulator runtime. The project is generated from
`project.yml` (`xcodegen`); the generated `StarkPocket.xcodeproj` is committed, so XcodeGen
is only needed after editing `project.yml`.

```sh
xcodebuild -project ios/StarkPocket/StarkPocket.xcodeproj -scheme StarkPocket \
  -destination 'platform=iOS Simulator,name=iPhone 18 Pro' test -only-testing:StarkPocketTests
ios/scripts/pocket-demo.sh            # live: runtime, StarkMac, simulator, faults
ios/scripts/pocket-demo.sh --skip-faults
ios/scripts/pocket-demo.sh --tailscale --skip-faults \
  --inference-url http://127.0.0.1:30000/v1 --model mlx-community/Qwen3-4B-4bit \
  --inference-extra '{"chat_template_kwargs": {"enable_thinking": false}}'   # a real model
```

`pocket-demo.sh` uses `~/.gameshark/demo` (fresh each run) and writes
`pocket-acceptance.md`, StarkMac snapshots, and simulator screenshots there. Without
`--inference-url` it asks the runtime for the fake model. Keep `--skip-faults` against a
real server: the faults SIGKILL the runtime mid stream, and the local SGLang server has
answered later identical prompts with another request's text after aborted streams.

These are the automated tests, and they run in the simulator. The physical iPhone results
under On a physical iPhone are the user's own observations by hand, not automated runs.

## Lifecycle

Foreground: connect, authenticate, resync rows after the last contiguous seq, resend any
unsent message under its original id. Background: give a pending send up to 3 s, then
close cleanly; no background modes, no daemon, no overlay. Stark stays on the phone while
it is backgrounded; Jack In Here on the Mac takes him back (the silent phone is committed
past after the 3 s prepare deadline). A jack still open when the app leaves reverts.
A change of network class (Wi-Fi, cellular, offline) redials at once, LAN first at home
and the Mac's Tailscale address first away; a reconnect retries the route that last
worked. The first dial waits for the phone's first network report (at most 1 s), so a
launch on cellular tries the tailnet before any LAN address. A send the socket never
drains gives up after 5 s and redials; it never holds a network change, a reconnect, or
the app leaving. A revoked device drops to the pairing screen.

## On a physical iPhone

A physical iPhone has passed the Phase 3.3 cellular acceptance by hand; see Over cellular
with Tailscale. It has also passed the Phase 4.1 live inference acceptance by hand; see
Live inference (Phase 4.1). It has also passed the Phase 4.2 saved memory acceptance by
hand; see Saved memory (Phase 4.2). It has also passed the Phase 4.3A persona acceptance
by hand; see Persona (Phase 4.3A). It has also passed the Phase 4.3B voice output
acceptance by hand; see Voice output (Phase 4.3B). The LAN checklist below has still not run end to end on a
device. The
checklist uses its own database, never `~/.gameshark/runtime.db` or the simulator's
`~/.gameshark/demo`. Run the Mac commands from `prototype/`.

```sh
export DB=~/.gameshark/device-check/runtime.db && mkdir -p "$(dirname "$DB")"
uv run python stark.py --db "$DB" run --lan --fake-model   # terminal 1; allow incoming connections
STARK_SOCKET="$(dirname "$DB")/gateway/stark.sock" \
  "$(../mac/scripts/build-app.sh | tail -1)/Contents/MacOS/StarkMac" &   # this database's StarkMac
uv run --with qrcode==8.0 python stark.py --db "$DB" pair --name "My iPhone"
```

1. Signing: open `StarkPocket.xcodeproj`, StarkPocket target, Signing & Capabilities,
   choose your personal team. Put the iPhone and the Mac on the same Wi-Fi; run the app
   on the iPhone from Xcode.
2. Pair: scan the QR with the iPhone Camera and open it in Stark Pocket; allow local
   network access. Expect "Stark is on Mac".
3. Jack In, talk, Jack Out: tap Jack In (StarkMac dims), send a message, see the reply on
   the phone, tap Jack Out (StarkMac active, same conversation).
4. Drafts both ways: type text in StarkMac without sending, then Jack In on the phone;
   expect the text in the phone's composer, unsent, and StarkMac's composer empty. Type
   text on the phone without sending, then press Jack In Here in StarkMac's menu; expect
   it in StarkMac's composer, unsent, and the chat card not taking the keyboard. Type
   different text in StarkMac first and repeat: expect a Use It / Keep Mine choice, never
   an overwrite. `sqlite3 "$DB" "SELECT draft, draft_state FROM handoffs"` shows each
   carried draft `delivered`; `uv run python stark.py --db "$DB" drafts` lists any that
   are not.
5. Re-pair refused: while paired, open a fresh `stark.py pair` link on the phone. Expect
   "already paired"; the phone stays connected.
6. Unpair while holding: Jack In, then Settings, Unpair This iPhone. Expect "Sending Stark
   home…", StarkMac active, then the pairing screen. Repeat with Wi-Fi off on the phone:
   expect the pairing kept with a recovery message and Forget Anyway.
7. SIGKILL: pair again, Jack In, then `kill -9` the daemon and restart it with the same
   command. Expect the phone to reconnect with Stark still here.
8. Revoke mid jack home: tap Jack Out and run
   `uv run python stark.py --db "$DB" devices revoke <id>` at once. Expect StarkMac to hold
   Stark and the phone to return to pairing, never Stark back on the phone.
9. Stale endpoint: stop the daemon with `kill -9`, then run `stark.py pair` again. Expect a
   refusal, no link.

### Over cellular with Tailscale

#### Phase 3.3 acceptance: PR #32 build, 2026-10-16

User observed on a physical iPhone, by hand; not automated test results. Environment:

- iPhone running the Stark Pocket build from PR #32 (first path wait, 5 s send deadline).
- Runtime from `main` at 31c7926: `uv run python stark.py --db "$DB" run --lan --tailscale`.
- Mac tailnet endpoint 100.123.57.23:47821; iPhone tailnet address 100.123.82.32.
- The existing device check database; one Stark identity throughout.

Observed:

1. The iPhone made an authenticated Tailscale connection on cellular.
2. Jack In and Jack Out worked over cellular.
3. A message sent over cellular got its reply.
4. Switching Wi-Fi to cellular and cellular to Wi-Fi kept the session working.
5. A message sent during a network switch arrived with one observed reply and no observed
   duplicate.
6. Stark's active presence stayed consistent across every network change.
7. Turning Tailscale off made the runtime unreachable for a while.
8. Turning Tailscale back on restored communication.
9. An unsent draft held in memory survived the disconnect and reconnect.
10. The recovered message arrived without an observed duplicate, and Jack Out then
    succeeded.

Runtime log:

| Time | Event |
|---|---|
| 15:04:59 | authenticated via tailnet, session s2 |
| 15:09:47 | moved to LAN: s5 superseded tailnet session s4 |
| 15:10:38 | moved to tailnet: s6 superseded LAN session s5 |
| 15:12:04 | tailnet restored: s7 superseded s6 |

Phase 3.3 is accepted for these demonstrated scenarios. One run by hand is not a claim of
production reliability or of exactly once delivery in general.

Not yet proven on physical hardware:

- Cold launch timing on the tailnet first path, and the route order of the first dial.
- A deliberately stalled TCP send reaching the 5 s timeout.
- A quick background and foreground during the first 1 s network path wait.
- Every fault and revocation case (LAN checklist steps 5 to 9 above, cellular step 9).
- Unsent composer text surviving app termination (only the in memory draft was shown).

#### Earlier diagnostic build 176cb4dd0ea3, 2026-10-16

Kept as history. User observed by hand on an iPhone over 5G with Tailscale, on the build
before the first path and send deadline fixes:

- Jack In and Jack Out over 5G plus Tailscale.
- A message sent over cellular, and its reply.
- One conversation across StarkMac and Stark Pocket.
- Wi-Fi to cellular and back without losing presence or an unsent draft.
- Tailscale down and up without losing presence or an unsent draft.
- No duplicate messages seen in these tests.

#### Checklist

Needs Tailscale signed in to the same tailnet on the Mac and the
iPhone. The checklist below pairs over Wi-Fi first, so the link carries both the LAN and
the tailnet endpoints.

```sh
uv run python stark.py --db "$DB" run --lan --tailscale --fake-model   # terminal 1; expect "tailnet gateway on 100.x:47821"
tailscale serve status                                    # expect no handlers: the runtime refuses to serve otherwise
uv run --with qrcode==8.0 python stark.py --db "$DB" pair --name "My iPhone"
```

1. Pair on home Wi-Fi, as above. The link prints a line naming the Tailscale endpoint.
2. iPhone to cellular: Settings, Wi-Fi off; Tailscale app connected. Expect Stark Pocket to
   reconnect within seconds; the runtime log shows `device … via tailnet from 100.…`.
3. Jack In over cellular; StarkMac dims.
4. Send a message, see the reply on the phone.
5. Jack Out; StarkMac active, same conversation.
6. Switch mid session: Jack In, type a draft without sending, then turn Wi-Fi on. Expect a
   `via lan` connection and a `closed …: moved to lan` line, Stark still on the phone, the
   draft still in the composer. Turn Wi-Fi off again and expect `via tailnet`.
7. Send while switching: tap Send and turn Wi-Fi off at once. Expect the message once.
8. Recovery evidence, after all of the above:
   `sqlite3 "$DB" "SELECT COUNT(*), COUNT(DISTINCT seq) FROM surface_writes"` (equal),
   `sqlite3 "$DB" "SELECT COUNT(*) FROM devices WHERE revoked_at IS NULL"` (1),
   `sqlite3 "$DB" "SELECT id, state FROM handoffs"` (only the jacks you made), and
   `uv run python stark.py --db "$DB" drafts` (nothing unconfirmed).
9. Tailscale off on the Mac (`tailscale down`), then `stark.py pair`: expect a refusal that
   names the stale tailnet endpoint. `tailscale up` and restart the daemon to serve it again.

If step 2 fails, the runtime log says how far the phone got. No `rejected tailnet` line and
no `via tailnet` line means nothing reached the tailnet listener: check that the pairing
link printed a Tailscale endpoint (a phone paired before `--tailscale` holds LAN routes
only; unpair and pair again). `rejected tailnet … dropped`, `setup`, `tls`, or `auth`
names the stage that refused it. `netstat -Lan -p tcp | grep 4782` should show an empty
listen queue on both ports.

### Live inference (Phase 4.1)

#### Phase 4.1 acceptance: physical iPhone, 2026-10-17

User observed on a physical iPhone, by hand; not automated test results. Environment:

- Model: SGLang hosted on the Mac, serving `mlx-community/Qwen3-4B-4bit`.
- The runtime served genuine model replies through the PR #34 live inference path.
- One SQLite database for the whole run, including across the runtime restart.
- Not recorded: the exact commits of the runtime, StarkMac, and the iPhone build; the
  inference flags; the surface that asked the question in observation 7; whether the
  phone was on Wi-Fi or cellular for each step; how the phone was disconnected in
  observation 8; and the output of checklist step 7.

Observed:

1. StarkMac showed a genuine model reply from `mlx-community/Qwen3-4B-4bit`, not the fake
   model.
2. The user told Stark in the Mac conversation that their cat is named Biscuit.
3. After Jack In on Stark Pocket, the user asked "What's my cat's name?" on the phone and
   Stark answered Biscuit.
4. Stark kept one identity and one conversation across the Mac and the iPhone.
5. Jack Out returned active presence to the Mac.
6. The user restarted the GameShark runtime on the same SQLite database.
7. After the restart the user asked "What was my cat's name?" and Stark again answered
   Biscuit.
8. The user disconnected the iPhone while Stark was answering, restored connectivity, and
   saw the answer arrive once, with no observed duplicate.

Conversation history, not long term memory. Stark recalled Biscuit because the Mac turn naming
Biscuit is a row in `messages`, and every reply call sends the newest rows that fit the context
window (`journal.fit_history`). The restart rebuilt that window from the same SQLite rows.
This is ordinary conversation history retrieval, bounded by `--context-window`. Durable
long term semantic memory is not built: a fact that falls out of the window is gone, and
nothing extracts, indexes, or retrieves facts across conversations.

Phase 4.1 is accepted for these demonstrated scenarios. One run by hand is not a claim of
production reliability or of exactly once delivery in general.

Simulator and automated results, from PR #34 and unchanged here: the fake provider suites
(`pytest`, SwiftPM, the StarkPocket simulator tests, `demo.sh`, `pocket-demo.sh` with
faults); a scripted run against the real model on the Mac (Mac turn, phone turn over
pinned TLS answered `Biscuit.`); and `pocket-demo.sh --tailscale --skip-faults` with the
real model in the simulator, 5 of 5.

Not yet proven on physical hardware:

- Checklist step 6: the model server down, then back, with one answer.
- Checklist step 7: the SQL evidence for one call per question and no duplicate turn.
- The disconnect of observation 8 over a named route (cellular with Tailscale, or
  Wi-Fi), with the checklist step 7 queries confirming one call and one assistant row.
- SIGKILL of the runtime mid stream against a real server; on the Mac this stays limited by
  the SGLang prefix cache answering identical prompts with stale text after aborts.
- A reply that times out three times (AttemptsExhausted), then `retry_reply`.
- A message too long for the window (ContextTooLong).
- Jack In or Jack Out while a reply streams.
- Recall of a fact older than the context window (expected to fail; see above).

#### Checklist

Fake provider and simulator acceptance prove the pipeline; this checklist brings a real
model to a phone in your hand. Use the device check database and pairing from above
(Tailscale signed in on both).

```sh
# terminal 1: the model server (skip if already serving on :30000)
python -m sglang.launch_server --model-path mlx-community/Qwen3-4B-4bit --port 30000
curl -s http://127.0.0.1:30000/v1/models          # expect the model id

# terminal 2: the runtime, from prototype/
export GAMESHARK_INFERENCE_URL=http://127.0.0.1:30000/v1
export GAMESHARK_INFERENCE_MODEL=mlx-community/Qwen3-4B-4bit
export GAMESHARK_INFERENCE_EXTRA='{"chat_template_kwargs": {"enable_thinking": false}}'
uv run python stark.py --db "$DB" run --lan --tailscale   # expect "replies from mlx-community/Qwen3-4B-4bit at ..."

# terminal 3: StarkMac on this database
STARK_SOCKET="$(dirname "$DB")/gateway/stark.sock" \
  "$(../mac/scripts/build-app.sh | tail -1)/Contents/MacOS/StarkMac" &
```

1. Mac answer: in StarkMac ask something substantive ("My cat is Biscuit. Why do cats
   purr?"). Expect "Stark is thinking…" under it, then a model written answer with no
   `[fake model]` prefix and no fake model badge.
2. Jack In over cellular: iPhone Wi-Fi off, Tailscale on, open Stark Pocket, tap Jack In.
   StarkMac dims; the runtime log shows `via tailnet`.
3. Follow up needing context, from the phone: "What is my cat's name?" Expect "Biscuit".
4. Jack Out: StarkMac active, all four rows on both surfaces.
5. Disconnect during inference: on the phone ask something long ("Explain how TCP slow
   start works in five sentences."), and as soon as "Stark is thinking…" shows, turn on
   Airplane Mode. Wait 10 s, turn it off. Expect the answer once, on the phone and on the
   Mac; no second copy, no second question.
6. Model down (optional): stop the SGLang server, send a message. Expect "Waiting for the
   model: the model server is unreachable" and no answer text. Start the server again;
   within about 15 s the answer arrives once.
7. Evidence:

```sh
sqlite3 "$DB" "SELECT seq, role, substr(content, 1, 60) FROM messages
  WHERE conversation_id='stark:main' ORDER BY seq"                     # no duplicate turn
sqlite3 "$DB" "SELECT call_id, status, attempts FROM inference_calls
  ORDER BY created"                                                   # one done call per question
sqlite3 "$DB" "SELECT COUNT(*), COUNT(DISTINCT seq) FROM surface_writes"   # equal
uv run python stark.py --db "$DB" status                              # chat:stark:main done
```

### Saved memory (Phase 4.2)

#### Phase 4.2 acceptance: physical iPhone, 2026-10-17

User observed on a physical iPhone and StarkMac, by hand; not automated test results. The
full record, with what was and was not recorded, is in
[architecture/stark-memory.md](../../architecture/stark-memory.md#phase-42-acceptance-physical-iphone-2026-10-17).
Environment: a separate database, `~/.gameshark/memory-check/runtime.db`, with
`run --lan --fake-model` and a test identity beginning `9696f62c`. Not recorded: the iOS
version, the iPhone model, the build commit, the daemon log, and the SQL evidence.

Observed, checklist steps 1 to 9:

1. `/remember My locker is 42` from the iPhone appeared in Memories, apart from chat.
2. "Which locker is mine?" on the iPhone: the reply included `Saved memories: My locker is 42`.
3. After Jack Out, StarkMac had the same memory and its reply included locker 42.
4. With Stark on the Mac, the iPhone showed "Jack In to add or correct" and disabled Add
   and Edit; a left swipe still exposed Forget.
5. After Jack In, a correction to locker 43; later replies included locker 43.
6. SIGKILL, the stale lease expired, restart on the same database: the phone reconnected,
   the memory remained, and a reply included `Saved memories: My locker is 43`.
7. Forget: "Nothing saved yet.", and a notice of two earlier chat messages still containing
   the text.
8. "Which locker is mine now?": the reply carried no `Saved memories:` block.
9. Another SIGKILL and restart: the list stayed empty.

Step 10, revoking `memory.inject`, was not performed. Afterwards the user unpaired the test
iPhone, stopped the temporary runtime, and restored `~/.gameshark/device-check/runtime.db`:
the original identity (`b7c180ab`), its conversations, the Mac connection, and the iPhone
pairing all came back.

Phase 4.2 is accepted for these demonstrated scenarios. One run by hand is not a claim of
production reliability or of exactly once delivery in general. The fake model showed
retrieval and injection, not semantic reasoning by a real model. Forget removed the fact
from saved memory only, not from chat history, backups, snapshots, or provider caches.

#### Checklist

The phone stores no memory. The Memories sheet (brain button) lists what the Mac keeps,
`/remember <fact>` in the composer saves one, a tap corrects one, and a swipe forgets one.
Saving and correcting need Stark on this phone; forgetting works from either surface.
The ten step physical checklist (save, recall on both surfaces, the presence fence,
correct, SIGKILL restart, forget, recall after forget, restart again, injection revoked)
and the SQL to verify each step are in `architecture/stark-memory.md`. Forgetting removes
a fact from saved memory, not from chat history; the sheet says how many earlier messages
still quote it.

```
uv run python stark.py --db "$DB" memory                                # saved, with provenance
sqlite3 "$DB" "SELECT id, state, revision, length(content), source_surface FROM memories"
sqlite3 "$DB" "SELECT kind, surface FROM memory_ops ORDER BY created"   # remember, correct, forget
```

### Persona (Phase 4.3A)

#### Phase 4.3A acceptance: physical iPhone and StarkMac, 2026-10-18

User observed by hand; not automated test results. The full record is in
[architecture/stark-persona.md](../../architecture/stark-persona.md#phase-43a-acceptance-physical-iphone-and-starkmac-2026-10-18).
Environment: the original identity (`b7c180ab`) on `~/.gameshark/device-check/runtime.db`,
`mlx-community/Qwen3-4B-4bit` on the Mac, persona `stark/1`. Not recorded: the iOS
version, the iPhone model, the build commit, and the daemon log.

On the phone: Jack In continued the existing conversation without a reinstall, and Stark
kept a friendly, concise tone. Advice for a frozen application was short but lacked
technical precision. Asked what it remembered, Stark cited earlier network switch and
draft recovery testing, which the conversation history holds; this does not show saved
memory retrieval. SQLite recorded `stark/1` on the five most recent calls and on
`call:stark:main:37` after a graceful restart.

### Voice output (Phase 4.3B)

#### Phase 4.3B acceptance: physical iPhone and StarkMac, 2026-10-18

User observed by hand; not automated test results. The full record is in
[architecture/stark-voice-output.md](../../architecture/stark-voice-output.md#phase-43b-acceptance-physical-iphone-and-starkmac-2026-10-18).
Environment: `~/.gameshark/device-check/runtime.db` and a Qwen3 model on the Mac. Not
recorded: the iOS version, the iPhone model, the build commit, and the daemon log.

On the phone: Jack In moved speech to the iPhone while StarkMac stayed silent. Jack Out
during speech stopped audio at once, and StarkMac did not read that reply. In the
background Stark stayed silent, and on return no missed audio played. Force quitting and
reopening Stark Pocket replayed no earlier reply. One StarkMac relaunch was silent until
the voice was switched; that observation stays open.
