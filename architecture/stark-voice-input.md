# Stark voice input (Phase 4.3C)

Status: built. Automated tests pass on macOS and in the iOS simulator. Physical acceptance
has not run; the checklist is at the end. Nothing here is accepted until it passes on a
real Mac and a real iPhone.

## Goal

The user holds a microphone button on the surface where Stark is present, speaks, lets go,
reads and edits what was heard, and then chooses Send or Insert into Composer. Voice input
is a way to type. It adds no runtime command, no tool, and no authority: the runtime only
ever receives text the user explicitly submitted, through the same send path as typing.

Not in scope: wake words, background or ambient listening, server recognition, automatic
sending, saving audio, and voice commands that act without a Send.

## Privacy

- Recognition runs on device only. `SFSpeechInput` requires
  `supportsOnDeviceRecognition` and sets `requiresOnDeviceRecognition = true`. When on
  device English is not installed, push to talk is disabled with an explanation. It never
  falls back to Apple's servers.
- Audio goes from the microphone tap to the recognizer in memory. No audio, partial
  transcript, or voice draft is written to a file, UserDefaults, SQLite, the journal, or
  the log.
- The voice draft lives only in `CaptureCoordinator` on this device. It is not in
  `DraftBox`, so a jack never carries it, and `DraftContinuity` never sees it.
- Diagnostic lines (`capture ...`, category `speech`) carry sessions, reasons, and
  lengths, never words. A test pushes a sentinel through every path and finds none of it
  in any line.
- No gateway command exists for the microphone. `CaptureCoordinator` holds no
  `GatewayClient`; it reaches the runtime only through the model's `submit`, on Send.

## Permissions

Microphone and speech recognition access are requested on the first press, never at
launch. The press that raises the system prompt is spent: when the user answers, the
microphone stays closed and push to talk waits for a fresh, deliberate press. Denied and
restricted access disable the button with an explanation; typing keeps working.

| Platform | Keys |
|---|---|
| StarkMac | `NSMicrophoneUsageDescription`, `NSSpeechRecognitionUsageDescription` in the Info.plist `mac/scripts/build-app.sh` writes. The app is ad hoc signed without hardened runtime or sandbox, so it needs no entitlement. |
| Stark Pocket | The same two keys in `ios/StarkPocket/Info.plist` and `project.yml`. No signing, bundle identifier, or Xcode project change. |

A build without the usage strings (the bare SwiftPM binary) reports
`unavailable(not_configured)` and never touches the microphone.

## Code

| File (mac/StarkMac/Sources/StarkKit/Speech) | Role |
|---|---|
| `SpeechInput.swift` | `SpeechInput` protocol, `InputAvailability`, `InputEvent`. |
| `CaptureMachine.swift` | Pure state machine: presence gate, phases, sessions, effects. |
| `CaptureCoordinator.swift` | Main actor owner of machine, input, and Stark's voice hold; holds no client. |
| `SFSpeechInput.swift` | Speech framework recognizer and `AVAudioEngine` tap, on device only. |
| `AudioSessionClaims.swift` | iOS shared audio session owner for speech and capture. |

`StarkModel` and `PocketModel` each own one coordinator, feed it every `ClientState`, and
give it two closures: `submit` (their own send path, shared with typing) and `insert`
(append to the ordinary composer draft).

## Who may listen

Push to talk accepts a press only while this surface holds Stark: identity known,
connection live, `isActiveSurface`, presence known, and no jack open. A jack opening, a
presence generation change, a disconnect, or an identity change discards the voice draft
in any phase. During Jack In or Jack Out the button is disabled.

## State machine

| Phase | Press | Release | Partial / final | Leaves by |
|---|---|---|---|---|
| idle | ready: hold Stark, start session n. needs permission: prompt. | none | ignored | none |
| requesting | rejected (busy) | none | ignored | answer: back to idle, nothing starts |
| capturing(n) | rejected (busy) | finish audio, finishing(n) | session n updates the draft; final goes to review | cancel, interruption, failure, presence change |
| finishing(n) | rejected (busy) | none | session n final goes to review | 3 s timeout keeps the partial text |
| reviewing | another take, appended | none | ignored | Send, Insert, Discard, presence change, background |

Every result carries its session number. A partial or final from a cancelled or older
session is dropped twice: `SFSpeechInput` forgets the session on cancel, and the machine
applies an event only for its current session and phase. A recognizer failure or an
interruption discards what was heard; an empty result leaves nothing to review.

Send submits the trimmed text through `submit`; the draft clears only when the surface
accepted it, and a second tap finds nothing to send. A spoken `/remember` behaves exactly
as if typed. Insert appends the text to the composer, separated by a space, where draft
continuity then applies as for typing.

## Stark's voice and the audio session

Starting capture holds Stark's voice before the microphone opens: `SpeechMachine` gains a
`listening` disarm, independent of `suspended`, that stops the current utterance. Replies
that arrive while listening become history, so nothing plays when capture ends. The hold
lifts when capture ends, and the coordinator re-applies the last state, so replay is
offered again at once and the next new reply speaks as usual.

On iOS, speech output and capture share one `AVAudioSession` through
`AudioSessionClaims`. Capture uses `.record` / `.measurement` (no ducking option, which `.record` rejects; a capture never runs when that category cannot be set); speech alone keeps its
Phase 4.3B `.playback` / `.spokenAudio` session. The session deactivates only when the last
claim releases, so a stopped utterance that reports its end late cannot shut an open
microphone. An interruption, a lost route, a media server reset, or an audio engine
configuration change ends the capture and discards the draft. After a media server reset
the audio engine and recognizer are recreated, so the next press works without a relaunch.

Discarded by: Cancel or Discard, Escape (StarkMac), closing the chat card (StarkMac),
sleep (StarkMac), backgrounding (Stark Pocket), Jack In or Jack Out, unpairing, and any
presence or connection change. On Stark Pocket, the scene going inactive (Control Center,
the app switcher) ends an open microphone, since the system can end a hold without a
release; a permission prompt or a reviewed draft survives it. Opening Settings or Memories, which cover the
composer and its Stop Listening action, also ends an open microphone.

## Diagnostics

`capture` lines join the `speech` category (see
[stark-voice-output.md](stark-voice-output.md#speech-diagnostics)):

| Line | When |
|---|---|
| `capture start session=` | the microphone opens |
| `capture rejected reason=` | `not_present`, `busy`, or an availability such as `denied` or `unavailable_no_on_device` |
| `capture permission asked`, `capture permission result=` | the first press prompt and its answer |
| `capture end session= chars= [timed_out=true]` | a transcript is ready for review |
| `capture discard reason= chars=` | why a draft went away |
| `capture send chars=`, `capture insert chars=` | the explicit choices |
| `capture input failed code=` | the microphone or recognizer could not start; the capture is discarded |
| `capture input rebuilt after media reset` | iOS reset its media services; the engine and recognizer were recreated |
| `listening on`, `listening off`, `disarmed reason=listening` | Stark's voice hold |

## Known limitations

- On device English recognition must be installed. On a Mac set to another language,
  enable English dictation (System Settings, Keyboard, Dictation, add English) so
  `supportsOnDeviceRecognition` reports true; recognition always uses en-US.
- StarkMac is ad hoc signed, so macOS may ask for microphone access again after each
  rebuild. `tccutil reset Microphone dev.gameshark.starkmac` clears a stuck denial.
- Hold to talk needs a pointer or touch. VoiceOver users get Start and Stop Listening
  actions instead of a hold. A hold the system cancels (a permission prompt, Control Center)
  counts as a release, so the button never stays latched.
- Escape reaches the chat card only while the panel has keyboard focus; Discard and
  closing the card always work.
- Apple caps a single recognition request at about a minute; a longer hold ends with what
  was heard.
- StarkMac launched by running its executable from a terminal gets the terminal's
  microphone permission, not its own; launch it with `open`.
- macOS has no audio session interruption notice; StarkMac ends a capture on sleep, on an
  audio device change, and on its controls.
- The microphone may pick up other people nearby; the review step is the safeguard.

## Tests

`mac/StarkMac/Tests/StarkKitTests/CaptureTests.swift`, fake recognizer and engine:

- **Permissions:** the first press prompts and starts nothing; a grant needs a fresh press; denied and restricted never prompt; no on device recognition, no recognizer, not configured, and offline stay off.
- **Transcription:** partial and final fill the draft; release keeps it for review; edits apply only in review; a second take appends; a final while held ends capture; an empty result leaves nothing; a finish timeout keeps the partial.
- **Discards:**
  - Cancel and Escape discard, and late callbacks change nothing.
  - An older session's callback never reaches the new one.
  - Background, sleep, and chat close discard while listening and in review.
  - A jack opening discards and blocks pressing.
  - A generation change discards a reviewed draft.
  - A ghost cannot listen.
  - A disconnect discards during recognition and review.
  - Interruption and failure discard.
  - A stale state never discards.
- **Send and insert:** Send submits exactly once; a refused submit keeps the draft; nothing submits while listening; Insert hands over the text once.
- **Speech:** the engine stops before the microphone starts; a reply during capture is never spoken and nothing resumes; replay returns at once; a discarded capture releases the voice.
- **Privacy:** no line carries transcript words; the coordinator holds no client.
- **Session and machine:**
  - `AudioSessionClaims`: speech alone matches Phase 4.3B; a late speech release never deactivates an open microphone; a failed activation holds no claim.
  - `SpeechMachine`: the listening disarm, and its independence from suspension.

`ios/StarkPocket/Tests/StarkPocketTests.swift`, `PocketVoiceInputTests` through
`PocketModel` and `FakeClient`:

- **Runtime:** capturing and editing send nothing to the runtime.
- **Send:** Send submits once and leaves the composer alone.
- **Continuity:** the voice draft never reaches the draft provider; Insert joins the composer; a carried draft still arrives.
- **Discards:** background, Jack Out, and a presence change discard.
- **Gating:** a phone without Stark cannot listen; speech stops when listening starts and a reply meanwhile stays silent.

## Physical acceptance checklist

Not run. Each step needs StarkMac and Stark Pocket built from this branch, the runtime on
the existing device check database, and the existing pairing; no reinstall or new pairing.

Setup, from `prototype/` in this branch's checkout:

```sh
export DB=~/.gameshark/device-check/runtime.db
sqlite3 "$DB" "PRAGMA quick_check; SELECT count(*) FROM messages"      # note the count
uv run python stark.py --db "$DB" run --lan \
  --inference-url http://127.0.0.1:30000/v1 --model mlx-community/Qwen3-4B-4bit \
  --inference-extra '{"chat_template_kwargs": {"enable_thinking": false}}'   # terminal 1
../mac/scripts/build-app.sh
open -n --env STARK_SOCKET="$(dirname "$DB")/gateway/stark.sock" ../mac/build/StarkMac.app
/usr/bin/log stream --level info \
  --predicate 'subsystem == "dev.gameshark.starkmac" AND category == "speech"'   # terminal 2
```

Launch StarkMac with `open`, not by running its executable from a terminal: macOS
attributes microphone access to the app that launched it, so a terminal launch asks for
the terminal's access instead of StarkMac's.

Stark Pocket: in Xcode open `ios/StarkPocket/StarkPocket.xcodeproj`, scheme StarkPocket,
your iPhone as destination, your personal team under Signing & Capabilities, bundle
identifier `dev.gameshark.starkpocket` unchanged, then Run. Settings shows the build
commit.

1. **First press, Mac.** With Stark on the Mac, hold the microphone. The system asks for
   microphone and speech recognition access. Allow both. Confirm nothing is recording
   (no orange microphone indicator in the menu bar) until you press again.
2. **Dictate and review, Mac.** Hold, say a sentence, release. The text appears in the
   voice draft, editable. Confirm nothing was sent (no new user row, no reply).
3. **Edit and Send, Mac.** Change a word and press Send. Exactly one user row with the
   edited text appears, and Stark replies.
4. **Insert, Mac.** Dictate, press Insert into Composer. The text lands in the composer;
   send it with Return.
5. **Escape and Discard, Mac.** Hold and speak, press Escape while holding: the draft
   vanishes and the microphone indicator goes off. Dictate again and press Discard.
6. **Speech stops, Mac.** While Stark is reading a reply aloud, hold the microphone: he
   stops before you speak, and he does not resume after release.
7. **Read only surface.** With Stark on the iPhone, the Mac's microphone is disabled; the
   same holds for the phone when Stark is on the Mac.
8. **First press, iPhone.** Jack Out to the phone. Hold the microphone; allow both
   prompts. Confirm capture starts only on a second press.
9. **Dictate, Send, Insert, iPhone.** Repeat steps 2 to 4 on the phone.
10. **Background, iPhone.** Hold and speak, then swipe home. Return: no draft, and nothing
    was sent.
11. **Interruption, iPhone.** Start a capture and trigger an interruption (an incoming
    call or a timer alarm). The capture ends and the draft is discarded.
12. **Jack during review.** Leave a reviewed voice draft on the phone and Jack In on the
    Mac. The phone's draft is discarded, the Mac's composer does not receive it, and
    nothing is sent.
13. **Denied access.** Turn the app's microphone access off in Settings. The button is
    disabled with an explanation, and typing still works. Turn it back on.
14. **On device only.** With on device English unavailable, if you can arrange it, the
    button is disabled with the on device explanation. Optional.
15. **Database.** After the run, `PRAGMA quick_check` is `ok`, user rows grew only by the
    turns sent, and no audio or transcript appears in the database or the journal.

Phase 4.3B checks still outstanding and not claimed here: the dedicated Stop button
stopping a second replay, and Jack In interrupting StarkMac during active speech.
