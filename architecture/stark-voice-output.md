# Stark voice output (Phase 4.3B proposal)

Status: design only. Nothing here is built. Phase 4.3A physical acceptance is recorded in
[stark-persona.md](stark-persona.md#phase-43a-acceptance-physical-iphone-and-starkmac-2026-10-18).

## Goal

Stark reads his new replies aloud on the surface he is present on. Speech is a rendering
of committed assistant rows, like the chat bubble: it adds no tools, no authority, and no
writes to SQLite memory or the journal.

## Engine

Device native first: `AVSpeechSynthesizer` on macOS and iOS, offline, no new runtime
dependency, no audio leaving the device. Behind one protocol so a neural engine can
replace it later without touching the surfaces:

```swift
protocol SpeechEngine: AnyObject {
    func speak(_ utterance: Utterance)      // id = (conversation_id, seq)
    func stop()
    var onFinish: ((Utterance.ID) -> Void)? { get set }
}
```

Voice choice (language, rate, pitch) is a per device setting, not part of the persona.
Persona text stays the one source of Stark's character; the voice only reads it.

## What speaks

- Only assistant rows that arrive live after the surface synced, never rows from history
  catch up, a page fetch, or a reconnect replay.
- Only on the surface that holds presence (the authorized active surface). A surface that
  is not present stays silent even when it renders the row.
- One utterance per row. Duplicate suppression keys on `(conversation_id, seq)` and
  persists the last spoken seq per device (UserDefaults), so a reconnect, a resync, or an
  app relaunch never repeats a reply.

## Controls

- Mute: a per device toggle; muted rows count as spoken, so unmute never plays a backlog.
- Replay: a button on an assistant bubble speaks that row once on demand.
- Stop: tapping the avatar or sending a new message stops current speech.

## Interruptions and presence

- Audio session: `.playback` with `.duckOthers` on iOS; on an interruption (call, Siri,
  another app) stop, do not resume; on route change to no output, stop.
- Jack In and Jack Out: stop playback on both surfaces when a handoff starts; the new
  surface speaks only rows that commit after its `ack_in`.
- Runtime restart or gateway disconnect: stop; resume speaking only new rows after resync.

## Out of scope

Voice input, wake words, background listening, server side synthesis, and any action
taken because of speech.

## Tests to write first

Duplicate suppression across reconnect and relaunch; silence on a non present surface;
stop on handoff start; mute marks rows spoken; replay speaks exactly once; a fake
`SpeechEngine` proves no SQLite write or gateway command originates from speech.
