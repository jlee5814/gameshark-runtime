# Stark voice output (Phase 4.3B)

Status: built. Automated tests pass on macOS and in the iOS simulator. Physical device
acceptance is pending; nothing below claims it.

## Goal

Stark reads his new replies aloud on the surface he is present on. Speech is a rendering
of committed assistant rows, like the chat bubble: it adds no tools, no authority, and no
writes to SQLite memory or the journal. It sends no gateway command and starts no
inference.

## Engine

`AVSpeechSynthesizer` on macOS and iOS: on device, offline, no new runtime dependency, no
audio leaving the device. The surfaces talk to one protocol, so a neural engine can
replace it later without touching them:

```swift
@MainActor protocol SpeechEngine: AnyObject {
    func speak(_ u: Utterance)          // id = (conversation, seq, token)
    func stop()
    var onFinish: ((Utterance.ID) -> Void)? { get set }
    var onInterrupt: (() -> Void)? { get set }
}
```

`AVSpeechEngine` (StarkKit/Speech) is the shipped conformer. The text it speaks is the
row's content exactly as the runtime committed it; nothing rewrites the model's words to
suit the synthesizer.

Voice settings are per device: mute, voice, speed, and pitch, kept in UserDefaults under
`stark.voice.settings`. They are not part of the persona and never reach the runtime. The
persona remains the one source of Stark's character; the voice only reads it.

## Code

| File (mac/StarkMac/Sources/StarkKit/Speech) | Role |
|---|---|
| `SpeechMachine.swift` | Pure state machine: gate, watermark, effects. Most tests target it. |
| `SpeechCoordinator.swift` | Main actor owner of machine, engine, and store. Holds no `GatewayClient`. |
| `SpeechEngine.swift` | Engine protocol, `Utterance`, `VoiceSettings`, the store, the ledger key. |
| `AVSpeechEngine.swift` | AVFoundation engine, iOS audio session, voice catalogue. |

`StarkModel` (StarkMac) and `PocketModel` (Stark Pocket) each own one coordinator and feed
it every `ClientState` they apply. `ClientState.revision`, stamped by `GatewayClient` on each
publish and rising across every client in the process, lets the machine drop a state that
arrives after a newer one (Stark Pocket hops states to the main actor through separate
tasks).

## The gate

A surface speaks an assistant row on its own only while armed:

- the identity is known,
- the connection is live,
- this surface holds presence,
- no jack is open (`handoff` is nil),
- the device is not muted,

and only when the row's seq is above the handled watermark. Every time the surface arms,
the watermark rises to the newest row it holds. In one state update, only the newest new
assistant row speaks; every new seq counts as handled.

Consequences:

- History, sync catch up, reconnect replay, a gap refill, and relaunch never speak: those
  rows are at or below the watermark set when the surface armed.
- Out of order rows: seq 12 before seq 11 speaks 12; 11 arrives below the watermark and
  stays silent.
- Mute advances the watermark without speaking, so unmute plays no backlog.
- The watermark persists before the engine speaks (at most once): a crash or force quit
  mid sentence never replays that reply.

### Why two devices never speak one reply

The runtime pushes `presence` before `messages` before `activity` on every pump, on every
connection (`surface_api._push_changes`). So each surface learns of an open jack, or of
presence leaving it, before it can see any row committed after that change:

- The source disarms (and stops) when the jack opens.
- The target arms only after the jack closes, raising its watermark to the newest row, so it
  never speaks a reply the source already spoke.
- A reply that commits while a jack is open is silent everywhere. Replay reads it on demand.

### Ledger

```
stark.voice.handled.<agent uuid>.<conversation id>.<surface>
```

One integer per agent, conversation, and device surface, in UserDefaults. A test runtime
(another database, another uuid) keeps its own mark and never moves the user's own
Stark's. An identity change stops speech and loads the new identity's mark.

### State machine

| Event | Effect |
|---|---|
| state: identity changed | stop; load that identity's watermark |
| state: offline, ghost, or jack open | disarm; stop |
| state: arms (or presence generation changed) | stop; watermark = newest row |
| state: armed, new rows | watermark = newest; a new user row stops; newest assistant row speaks unless muted |
| state: older revision than one seen | ignored |
| mute | stop; later rows are handled silently |
| replay(seq) | only while armed: stop, speak that row once; watermark unchanged |
| suspend (Stark Pocket leaves the foreground) | disarm; stop; rows that land before return stay unspoken |
| stop (Stop button, avatar tap, user sends) | stop |
| interrupted (audio session, route loss, sleep) | stop; never resumes |
| finished(id) | clears speaking only when id is the current utterance |

Replay is offered only where Stark could speak on his own. A read only surface renders
rows and never acts, speech included.

## Controls

| | StarkMac | Stark Pocket |
|---|---|---|
| Mute | speaker button in the chat card; Voice menu | speaker button in the toolbar; Settings, Voice |
| Stop | stop button in the chat card; click Stark; Voice menu; sending | stop button in the toolbar; tap Stark; sending; Jack In or Out |
| Replay | play button beside each Stark bubble | play button beside each Stark bubble; long press, Read Aloud |
| Voice | Voice menu: installed voices | Settings, Voice picker |
| Speed and pitch | Voice menu: Slower, Normal, Faster | Settings, Speed and Pitch sliders |

Each control has an accessibility label; the avatar has a Stop Speaking accessibility
action on both surfaces.

## Interruptions and presence

- iOS audio session: `.playback`, mode `.spokenAudio`, `.duckOthers`; active only while
  Stark speaks, released with `.notifyOthersOnDeactivation`. An interruption that begins
  (a call, Siri, an alarm), a route change for `.oldDeviceUnavailable` (headphones
  unplugged), or a media services reset stops speech; nothing resumes.
- Stark Pocket suspends speech when the app leaves the foreground, including the up to 3 s
  a pending send keeps the connection open; there is no background audio mode.
- StarkMac stops speech when the Mac goes to sleep.
- Jack In and Jack Out stop speech on the surface that starts the jack at once, and on
  every surface when the runtime reports the jack open.
- Runtime restart or gateway disconnect: the surface disarms and stops; after resync it
  speaks only rows that commit later.

## Voice choice

`VoiceCatalog` offers installed English voices, best quality first, and leaves out novelty
voices (bells, whispers, robots), Personal Voice, and Siri voices (listed by the API but not
reliably available to apps). The default is the best installed copy of the first of
Evan, Nathan, Aaron, Alex, Tom, Reed, Eddy, Rocko; failing those, the best voice that is
not an elder or novelty character. On a Mac whose system language is not English, the
catalogue uses en-US. Rate defaults to 1.05 times the system rate and pitch to 1.05, inside
bounded ranges (rate 0.85 to 1.2, pitch 0.9 to 1.2).

## Known limitations

- Native voices set the ceiling. No installed system voice sounds like a youthful
  NetNavi; the defaults stay near natural rate and pitch rather than forcing a cartoon.
  Enhanced and premium voices (Evan, Nathan) sound better but must be downloaded by the
  user (macOS: System Settings, Accessibility, Spoken Content, System Voice, Manage Voices;
  iOS: Settings, Accessibility, Spoken Content, Voices). A developer Mac without them
  falls back to Reed, an older formant voice.
- A reply that commits while a jack is open is never spoken on its own.
- macOS has no audio session interruption notice; StarkMac stops on sleep, disconnect, and
  the controls, not when another app takes the speakers.
- Markdown and symbols are read as the model wrote them.
- `.playback` speaks even with the iPhone ring switch set to silent; mute is the in app
  control.
- One English voice for every reply; a reply in another language is read with English
  pronunciation.

## Out of scope

Voice input, wake words, microphone recording, background listening, server side
synthesis, cloud TTS, and any action taken because of speech. Phase 4.3C (voice input) is
not started.

## Tests

`mac/StarkMac/Tests/StarkKitTests/SpeechTests.swift` (fake engine, in memory store):
one utterance per new reply; silent history; no replay after reconnect, relaunch, or a
kill mid sentence; silence on a read only surface; stop when a jack opens and target
speaks only later replies; six rapid handoffs with no reply spoken twice; mute without
backlog; replay once, never on a ghost; Stop, sending, and a user row stop speech; audio
interruption without resume; runtime restart; out of order and batched rows; a stale
snapshot; identity change with separate ledgers; a stale finish; settings clamped and
carried; default voice choice. `SpeechWritesNothingTests` drives `GatewayClient` against
the fake runtime and proves speaking, muting, replaying, stopping, and interruptions add
no request.

`ios/StarkPocket/Tests/StarkPocketTests.swift`, `PocketVoiceTests`: new reply spoken once
with silent history, silence on a read only phone, sending stops speech, Jack Out stops
speech, leaving the foreground stops speech, a reply during the background send grace stays
silent, replay and mute, and no client call from
speech.

## Physical acceptance checklist (pending)

Not yet run. Use the device check database from the Stark Pocket README and the existing
pairing; no reinstall and no new pairing are needed. Run the Mac commands from
`prototype/`.

```sh
export DB=~/.gameshark/device-check/runtime.db
uv run python stark.py --db "$DB" run --lan \
  --inference-url http://127.0.0.1:30000/v1 --model mlx-community/Qwen3-4B-4bit \
  --inference-extra '{"chat_template_kwargs": {"enable_thinking": false}}'   # terminal 1
STARK_SOCKET="$(dirname "$DB")/gateway/stark.sock" \
  "$(../mac/scripts/build-app.sh | tail -1)/Contents/MacOS/StarkMac" &
sqlite3 "$DB" "SELECT count(*) FROM messages; SELECT count(*) FROM memories WHERE state='active'"
```

Build Stark Pocket from Xcode onto the iPhone: open `ios/StarkPocket/StarkPocket.xcodeproj`,
scheme StarkPocket, your iPhone as destination, the same personal team under Signing &
Capabilities, bundle identifier `dev.gameshark.starkpocket` unchanged, then Run. Settings
shows the build commit.

1. StarkMac holds Stark. Send a message; hear exactly one Qwen3 reply, once.
2. Click the speaker button: muted. Send a message; the reply appears and stays silent.
   Unmute; nothing plays. Click the play button beside an older Stark bubble; it plays
   once. Click Stark mid sentence; it stops. Replay again and press the stop button; it
   stops.
3. On the iPhone tap Jack In. If StarkMac is speaking, it stops the moment the jack
   begins.
4. On the iPhone send a message; hear the fresh reply on the phone.
5. StarkMac shows the same reply and stays silent.
6. Ask for a long answer on the iPhone; while it speaks, tap Jack Out. The phone stops at
   once; StarkMac does not read that reply.
7. With StarkMac holding Stark and speaking, press Control C in terminal 1 and start the
   runtime again (or turn Wi-Fi off on the phone while it speaks, then on). Speech stops;
   after reconnect nothing is repeated.
8. Quit StarkMac and relaunch it with the same command; force quit Stark Pocket and open
   it again. Neither reads any earlier reply.
9. Run the `sqlite3` line again: the message count grew only by the turns you sent and
   their replies, and the active memory count is unchanged. Scroll each conversation: the
   earlier history is intact, and Memories lists the same saved facts.
