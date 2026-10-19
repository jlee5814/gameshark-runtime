import AVFoundation
import Foundation
import Testing
@testable import StarkKit

/// Records what the coordinator asks of the audio engine; plays nothing.
@MainActor
final class FakeSpeechEngine: SpeechEngine {
    var spoken: [Utterance] = []
    var stops = 0
    var playing: Utterance.ID?
    var onFinish: ((Utterance.ID) -> Void)?
    var onInterrupt: (() -> Void)?

    func speak(_ u: Utterance) {
        spoken.append(u)
        playing = u.id
    }

    func stop() {
        stops += 1
        playing = nil
    }

    /// The utterance reached its end.
    func finish() {
        guard let id = playing else { return }
        playing = nil
        onFinish?(id)
    }

    /// The system took the audio away: a call, Siri, unplugged headphones.
    func interrupt() { onInterrupt?() }

    var seqs: [Int] { spoken.map(\.id.seq) }
}

/// Builds the ClientState a GatewayClient would publish, revision by revision.
@MainActor
final class Feed {
    var state: ClientState
    /// GatewayClient revisions rise across every client in the process; so do these.
    private static var revision = 0

    init(surface: String = "mac", holder: String = "mac", uuid: String = "uuid-1",
         rows: [Row] = []) {
        state = ClientState(surface: surface)
        state.connection = .live
        state.identity = Identity(agentId: "stark", uuid: uuid)
        state.presence = PresenceInfo(surface: holder, home: "mac", generation: 1)
        state.rows = [Row(seq: 0, role: "system", content: "sys")] + rows
    }

    /// The next published state, after a change.
    func next(_ change: (inout ClientState) -> Void = { _ in }) -> ClientState {
        change(&state)
        Self.revision += 1
        state.revision = Self.revision
        return state
    }

    func add(_ role: String, _ text: String) -> ClientState {
        next { s in s.rows.append(Row(seq: s.rows.count, role: role, content: text)) }
    }

    func reply(_ text: String) -> ClientState { add("assistant", text) }
}

func history(_ n: Int) -> [Row] {
    (1...n).map { Row(seq: $0, role: $0 % 2 == 1 ? "user" : "assistant", content: "old \($0)") }
}

@MainActor
func speaker(_ store: MemorySpeechStore = MemorySpeechStore())
    -> (SpeechCoordinator, FakeSpeechEngine) {
    let engine = FakeSpeechEngine()
    return (SpeechCoordinator(engine: engine, store: store), engine)
}

@MainActor @Suite struct SpeechTests {
    @Test func aNewReplySpeaksOnceAndHistoryStaysSilent() {
        let (voice, engine) = speaker()
        let feed = Feed(rows: history(4))
        voice.observe(feed.next())
        #expect(engine.spoken.isEmpty)
        voice.observe(feed.add("user", "hi"))
        voice.observe(feed.reply("Hey! Ready when you are."))
        #expect(engine.seqs == [6])
        #expect(engine.spoken.first?.text == "Hey! Ready when you are.")
        #expect(voice.speakingSeq == 6)
        voice.observe(feed.next())                      // the same rows, published again
        #expect(engine.seqs == [6])
        engine.finish()
        #expect(voice.speakingSeq == nil)
    }

    @Test func theTextIsSpokenExactlyAsCommitted() {
        let (voice, engine) = speaker()
        let feed = Feed()
        voice.observe(feed.next())
        let text = "**On it.** Use `ls -la` -> then 2 + 2 = 4!"
        voice.observe(feed.reply(text))
        #expect(engine.spoken.map(\.text) == [text])
    }

    @Test func reconnectNeverRepeatsAReply() {
        let (voice, engine) = speaker()
        let feed = Feed()
        voice.observe(feed.next())
        voice.observe(feed.reply("one"))
        #expect(engine.seqs == [1])
        voice.observe(feed.next { $0.connection = .disconnected("runtime closed the connection") })
        #expect(engine.playing == nil)
        voice.observe(feed.next { $0.connection = .syncing })
        // A reply committed while the link was down arrives by sync: catch up, not news.
        voice.observe(feed.next { s in
            s.rows.append(Row(seq: 2, role: "assistant", content: "missed"))
        })
        voice.observe(feed.next { $0.connection = .live })
        #expect(engine.seqs == [1])
        voice.observe(feed.reply("fresh"))
        #expect(engine.seqs == [1, 3])
    }

    @Test func relaunchNeverReplaysHistory() {
        let store = MemorySpeechStore()
        let feed = Feed()
        do {
            let (voice, engine) = speaker(store)
            voice.observe(feed.next())
            voice.observe(feed.reply("before quit"))
            #expect(engine.seqs == [1])
        }
        let (voice, engine) = speaker(store)            // the app launched again
        // A reply landed while it was closed: the first live state already holds it.
        voice.observe(feed.next { s in
            s.rows.append(Row(seq: 2, role: "assistant", content: "while away"))
        })
        voice.observe(feed.next())
        #expect(engine.spoken.isEmpty)
        voice.observe(feed.reply("after launch"))
        #expect(engine.seqs == [3])
    }

    @Test func aKilledUtteranceIsNeverResumedAfterRelaunch() {
        let store = MemorySpeechStore()
        let feed = Feed()
        let (first, _) = speaker(store)
        first.observe(feed.next())
        first.observe(feed.reply("cut off mid sentence"))
        // No finish, no stop: the process died while speaking. The row was handled first.
        let (voice, engine) = speaker(store)
        voice.observe(feed.next())
        voice.observe(feed.next())
        #expect(engine.spoken.isEmpty)
    }

    @Test func aGhostSurfaceRendersButNeverSpeaks() {
        let (voice, engine) = speaker()
        let feed = Feed(holder: "pocket-a1b2c3")
        voice.observe(feed.next())
        voice.observe(feed.reply("for the phone"))
        #expect(engine.spoken.isEmpty)
        #expect(!voice.canReplay)
        voice.replay(seq: 1)
        #expect(engine.spoken.isEmpty)
    }

    @Test func aJackStopsSpeechAndTheTargetSpeaksOnlyLaterReplies() {
        let mac = Feed()
        let phone = Feed(surface: "pocket-a1b2c3")
        let (macVoice, macEngine) = speaker()
        let (phoneVoice, phoneEngine) = speaker()
        macVoice.observe(mac.next())
        phoneVoice.observe(phone.next())
        let reply = Row(seq: 1, role: "assistant", content: "long answer")
        macVoice.observe(mac.next { $0.rows.append(reply) })
        phoneVoice.observe(phone.next { $0.rows.append(reply) })
        #expect(macEngine.seqs == [1])
        #expect(macEngine.playing != nil)
        // Jack In on the phone: the open jack reaches both surfaces before any later row.
        let jack = HandoffInfo(id: "h1", from: "mac", to: "pocket-a1b2c3", state: "preparing",
                               fromGen: 1, toGen: nil)
        macVoice.observe(mac.next { $0.handoff = jack })
        phoneVoice.observe(phone.next { $0.handoff = jack })
        #expect(macEngine.playing == nil)
        let moved = PresenceInfo(surface: "pocket-a1b2c3", home: "mac", generation: 2)
        for (voice, feed) in [(macVoice, mac), (phoneVoice, phone)] {
            voice.observe(feed.next { $0.presence = moved })
            voice.observe(feed.next { $0.handoff = nil })
        }
        #expect(phoneEngine.spoken.isEmpty)             // never the reply the Mac spoke
        let fresh = Row(seq: 2, role: "assistant", content: "on the phone now")
        macVoice.observe(mac.next { $0.rows.append(fresh) })
        phoneVoice.observe(phone.next { $0.rows.append(fresh) })
        #expect(macEngine.seqs == [1])
        #expect(phoneEngine.seqs == [2])
    }

    @Test func rapidHandoffsNeverSpeakOneReplyTwice() {
        let mac = Feed()
        let phone = Feed(surface: "pocket-a1b2c3")
        let (macVoice, macEngine) = speaker()
        let (phoneVoice, phoneEngine) = speaker()
        let both = [(macVoice, mac), (phoneVoice, phone)]
        for (v, f) in both { v.observe(f.next()) }
        var gen = 1
        var seq = 0
        for round in 0..<6 {
            let to = round % 2 == 0 ? "pocket-a1b2c3" : "mac"
            let from = round % 2 == 0 ? "mac" : "pocket-a1b2c3"
            let h = HandoffInfo(id: "h\(round)", from: from, to: to, state: "preparing",
                                fromGen: gen, toGen: nil)
            seq += 1
            // A reply commits inside the jack window: silent everywhere.
            let during = Row(seq: seq, role: "assistant", content: "during \(round)")
            gen += 1
            let p = PresenceInfo(surface: to, home: "mac", generation: gen)
            for (v, f) in both {
                v.observe(f.next { $0.handoff = h })
                v.observe(f.next { $0.rows.append(during) })
                v.observe(f.next { $0.presence = p })
                v.observe(f.next { $0.handoff = nil })
            }
            seq += 1
            let after = Row(seq: seq, role: "assistant", content: "after \(round)")
            for (v, f) in both { v.observe(f.next { $0.rows.append(after) }) }
        }
        let all = macEngine.seqs + phoneEngine.seqs
        #expect(Set(all).count == all.count)
        #expect(all.count == 6)
        #expect(phoneEngine.seqs == [2, 6, 10])
        #expect(macEngine.seqs == [4, 8, 12])
    }

    @Test func mutedRepliesNeverPlayAsABacklog() {
        let (voice, engine) = speaker()
        let feed = Feed()
        voice.observe(feed.next())
        voice.observe(feed.reply("heard"))
        voice.setMuted(true)
        #expect(engine.playing == nil)                  // muting stops the current reply
        voice.observe(feed.reply("muted one"))
        voice.observe(feed.reply("muted two"))
        voice.setMuted(false)
        voice.observe(feed.next())
        #expect(engine.seqs == [1])
        voice.observe(feed.reply("after unmute"))
        #expect(engine.seqs == [1, 4])
    }

    @Test func muteIsAPerDeviceSettingThatSurvivesRelaunch() {
        let store = MemorySpeechStore()
        let (voice, _) = speaker(store)
        voice.setMuted(true)
        let (again, engine) = speaker(store)
        #expect(again.settings.muted)
        let feed = Feed()
        again.observe(feed.next())
        again.observe(feed.reply("quiet"))
        #expect(engine.spoken.isEmpty)
    }

    @Test func replaySpeaksTheChosenReplyOnceOnRequest() {
        let (voice, engine) = speaker()
        let feed = Feed(rows: history(4))
        voice.observe(feed.next())
        #expect(voice.canReplay)
        voice.replay(seq: 2)
        #expect(engine.seqs == [2])
        #expect(engine.spoken.first?.text == "old 2")
        voice.replay(seq: 1)                            // a user row is not Stark speaking
        voice.replay(seq: 99)                           // nothing there
        #expect(engine.seqs == [2])
        voice.observe(feed.next())
        #expect(engine.seqs == [2])                     // replay never queues more
        // Replaying while muted is an explicit request; it plays.
        voice.setMuted(true)
        voice.replay(seq: 4)
        #expect(engine.seqs == [2, 4])
    }

    @Test func replayDoesNotSwallowTheNextLiveReply() {
        let (voice, engine) = speaker()
        let feed = Feed(rows: history(2))
        voice.observe(feed.next())
        voice.replay(seq: 2)
        voice.observe(feed.reply("new"))
        #expect(engine.seqs == [2, 3])
        #expect(engine.stops >= 1)                      // the replay stopped for the new reply
    }

    @Test func stopAndSendingAMessageSilenceStark() {
        let (voice, engine) = speaker()
        let feed = Feed()
        voice.observe(feed.next())
        voice.observe(feed.reply("one"))
        voice.stop()
        #expect(engine.playing == nil)
        #expect(voice.speakingSeq == nil)
        voice.observe(feed.next())
        #expect(engine.seqs == [1])                     // a stopped reply never resumes
        voice.observe(feed.reply("two"))
        voice.userSent()
        #expect(engine.playing == nil)
        // The user's row lands, then the answer to it speaks.
        voice.observe(feed.add("user", "next question"))
        voice.observe(feed.reply("three"))
        #expect(engine.seqs == [1, 2, 4])
    }

    @Test func aUserRowFromTheRuntimeStopsSpeech() {
        let (voice, engine) = speaker()
        let feed = Feed()
        voice.observe(feed.next())
        voice.observe(feed.reply("one"))
        voice.observe(feed.add("user", "interrupting"))
        #expect(engine.playing == nil)
    }

    @Test func anAudioInterruptionStopsWithoutResuming() {
        let (voice, engine) = speaker()
        let feed = Feed()
        voice.observe(feed.next())
        voice.observe(feed.reply("one"))
        engine.interrupt()
        #expect(engine.playing == nil)
        #expect(voice.speakingSeq == nil)
        voice.observe(feed.next())
        #expect(engine.seqs == [1])
        voice.observe(feed.reply("two"))                // later replies still speak
        #expect(engine.seqs == [1, 2])
        voice.interrupted()                             // the Mac's sleep and output loss path
        #expect(engine.playing == nil)
    }

    @Test func aRuntimeRestartStopsSpeechAndResyncStaysSilent() {
        let (voice, engine) = speaker()
        let feed = Feed()
        voice.observe(feed.next { $0.activity = Activity(boot: "boot-1") })
        voice.observe(feed.reply("one"))
        voice.observe(feed.next { $0.connection = .disconnected("runtime stopping") })
        #expect(engine.playing == nil)
        voice.observe(feed.next { s in
            s.connection = .live
            s.activity = Activity(boot: "boot-2")
            s.rows.append(Row(seq: 2, role: "assistant", content: "answered at boot"))
        })
        #expect(engine.seqs == [1])
    }

    @Test func outOfOrderRowsSpeakOnlyTheNewest() {
        let (voice, engine) = speaker()
        let feed = Feed()
        voice.observe(feed.next())
        // seq 4 arrives before 2 and 3: the gap fills later and stays silent.
        voice.observe(feed.next { s in
            s.rows.append(Row(seq: 1, role: "user", content: "q"))
            s.rows.append(Row(seq: 4, role: "assistant", content: "newest"))
        })
        #expect(engine.seqs == [4])
        voice.observe(feed.next { s in
            s.rows = s.rows.filter { $0.seq < 4 } + [
                Row(seq: 2, role: "assistant", content: "older"),
                Row(seq: 3, role: "user", content: "q2"),
                Row(seq: 4, role: "assistant", content: "newest")]
        })
        #expect(engine.seqs == [4])
    }

    @Test func aBatchSpeaksOnlyTheNewestReply() {
        let (voice, engine) = speaker()
        let feed = Feed()
        voice.observe(feed.next())
        voice.observe(feed.next { s in
            s.rows.append(Row(seq: 1, role: "assistant", content: "a"))
            s.rows.append(Row(seq: 2, role: "assistant", content: "b"))
        })
        #expect(engine.seqs == [2])
    }

    @Test func aStaleSnapshotIsIgnored() {
        let (voice, engine) = speaker()
        let feed = Feed()
        voice.observe(feed.next())
        let early = feed.reply("one")                   // published first
        let jack = feed.next { $0.handoff = HandoffInfo(id: "h", from: "mac", to: "pocket",
                                                        state: "preparing", fromGen: 1,
                                                        toGen: nil) }
        voice.observe(jack)                             // delivered first
        voice.observe(early)                            // delivered late: older revision
        #expect(engine.spoken.isEmpty)
    }

    @Test func anIdentityChangeKeepsEachAgentsLedgerApart() {
        let store = MemorySpeechStore()
        let (voice, engine) = speaker(store)
        let mine = Feed(rows: history(2))
        voice.observe(mine.next())
        voice.observe(mine.reply("original"))
        #expect(engine.seqs == [3])
        let keyMine = SpeechLedger.key(agent: "uuid-1", conversation: Wire.conversation,
                                       surface: "mac")
        #expect(store.integer(keyMine) == 3)
        // Another runtime database (a test instance) answers the same app.
        let other = Feed(uuid: "uuid-test", rows: history(8))
        voice.observe(other.next())
        #expect(engine.playing == nil)
        voice.observe(other.next())
        voice.observe(other.reply("test agent"))
        #expect(engine.seqs == [3, 9])
        #expect(store.integer(keyMine) == 3)
        let keyTest = SpeechLedger.key(agent: "uuid-test", conversation: Wire.conversation,
                                       surface: "mac")
        #expect(store.integer(keyTest) == 9)
        #expect(keyMine != keyTest)
    }

    @Test func aSuspendedSurfaceStaysSilentAndNeverCatchesUp() {
        let (voice, engine) = speaker()
        let feed = Feed()
        voice.observe(feed.next())
        voice.observe(feed.reply("one"))
        voice.setSuspended(true)                        // the app left the foreground
        #expect(engine.playing == nil)
        // The connection lingers to finish a send; its reply lands while hidden.
        voice.observe(feed.add("user", "sent as the app left"))
        voice.observe(feed.reply("answer while hidden"))
        #expect(engine.seqs == [1])
        #expect(!voice.canReplay)
        voice.setSuspended(false)
        voice.observe(feed.next())
        #expect(engine.seqs == [1])                     // never a backlog on return
        voice.observe(feed.reply("after return"))
        #expect(engine.seqs == [1, 4])
    }

    @Test func aStaleFinishNeverEndsANewerUtterance() {
        let (voice, engine) = speaker()
        let feed = Feed()
        voice.observe(feed.next())
        voice.observe(feed.reply("one"))
        let first = engine.playing!
        voice.observe(feed.reply("two"))
        engine.onFinish?(first)                         // the cancelled one reports late
        #expect(voice.speakingSeq == 2)
    }

    @Test func voiceSettingsClampAndTravelWithEachUtterance() {
        let (voice, engine) = speaker()
        voice.update { $0.rate = 9; $0.pitch = 0.1; $0.voiceIdentifier = "com.example.voice" }
        #expect(voice.settings.rate == VoiceSettings.rateRange.upperBound)
        #expect(voice.settings.pitch == VoiceSettings.pitchRange.lowerBound)
        let feed = Feed()
        voice.observe(feed.next())
        voice.observe(feed.reply("hi"))
        #expect(engine.spoken.first?.voice == voice.settings)
    }
}

/// Collects diagnostic lines the way a device log would.
final class LineLog: @unchecked Sendable {
    private let lock = NSLock()
    private var all: [String] = []

    var lines: [String] { lock.withLock { all } }
    var trace: Trace { { [self] line in lock.withLock { all.append(line) } } }
}

@MainActor
func tracedSpeaker(_ store: MemorySpeechStore = MemorySpeechStore())
    -> (SpeechCoordinator, FakeSpeechEngine, LineLog) {
    let engine = FakeSpeechEngine()
    let log = LineLog()
    return (SpeechCoordinator(engine: engine, store: store, trace: log.trace), engine, log)
}

/// Diagnostics explain silence without changing what Stark says or ever logging it.
@MainActor @Suite struct SpeechDiagnosticsTests {
    @Test func aSpokenReplyLogsArmSpeakAndFinishInOrder() {
        let (voice, engine, log) = tracedSpeaker()
        let feed = Feed(rows: history(2))
        voice.observe(feed.next())
        voice.observe(feed.reply("Hey there"))
        engine.finish()
        #expect(log.lines == [
            "ready muted=false voice=default",
            "armed agent=uuid-1 surface=mac gen=1 handled=2",
            "speak seq=3 tok=1 origin=auto chars=9",
            "finished seq=3 tok=1",
        ])
    }

    @Test func aRejectedReplayLogsWhy() {
        let (voice, _, log) = tracedSpeaker()
        let feed = Feed(holder: "ios", rows: history(2))
        voice.observe(feed.next())
        voice.replay(seq: 2)
        #expect(log.lines.suffix(2) == ["disarmed reason=ghost",
                                        "replay rejected seq=2 reason=ghost"])
        voice.observe(feed.next { $0.presence = PresenceInfo(surface: "mac", home: "mac",
                                                             generation: 2) })
        voice.replay(seq: 99)
        #expect(log.lines.last == "replay rejected seq=99 reason=unknown_row")
    }

    @Test func aMutedReplyLogsTheSkip() {
        let (voice, engine, log) = tracedSpeaker()
        let feed = Feed()
        voice.observe(feed.next())
        voice.setMuted(true)
        voice.observe(feed.reply("quiet"))
        #expect(engine.spoken.isEmpty)
        #expect(log.lines.suffix(2) == ["muted on", "skip seq=1 reason=muted"])
    }

    @Test func steadyStatesLogTheGateOnce() {
        let (voice, _, log) = tracedSpeaker()
        let feed = Feed()
        for _ in 0..<20 { voice.observe(feed.next()) }
        #expect(log.lines.filter { $0.hasPrefix("armed") }.count == 1)
        #expect(log.lines.count == 2)
    }

    @Test func stopsNameTheirCauseAndWhatWasPlaying() {
        let (voice, engine, log) = tracedSpeaker()
        let feed = Feed()
        voice.observe(feed.next())
        voice.observe(feed.reply("one"))
        voice.stop()
        voice.stop()
        voice.observe(feed.reply("two"))
        voice.userSent()
        voice.observe(feed.reply("three"))
        engine.interrupt()
        voice.interrupted()
        #expect(log.lines.filter { $0.hasPrefix("stop") } == [
            "stop by=user seq=1 tok=1", "stop by=user idle", "stop by=send seq=2 tok=2",
            "stop by=audio seq=3 tok=3", "stop by=system idle",
        ])
    }

    @Test func handoffAndSuspensionLogTheGate() {
        let (voice, _, log) = tracedSpeaker()
        let feed = Feed()
        voice.observe(feed.next())
        voice.observe(feed.next { $0.handoff = HandoffInfo(id: "h1", from: "mac", to: "ios",
                                                           state: "preparing", fromGen: 1,
                                                           toGen: nil) })
        voice.observe(feed.next { $0.handoff = nil })
        voice.setSuspended(true)
        voice.setSuspended(true)
        #expect(Array(log.lines.dropFirst(2)) == [
            "disarmed reason=handoff",
            "armed agent=uuid-1 surface=mac gen=1 handled=0",
            "suspended on",
            "disarmed reason=suspended",
        ])
    }

    @Test func aStaleFinishIsLoggedAndChangesNothing() {
        let (voice, engine, log) = tracedSpeaker()
        let feed = Feed()
        voice.observe(feed.next())
        voice.observe(feed.reply("one"))
        let first = engine.playing!
        voice.observe(feed.reply("two"))
        engine.onFinish?(first)
        #expect(voice.speakingSeq == 2)
        #expect(log.lines.last == "finish stale seq=1 tok=1")
    }

    @Test func noLineEverCarriesTheText() {
        let secret = "Zebra launch code 7741 for Jae"
        let (voice, engine, log) = tracedSpeaker()
        let feed = Feed(rows: [Row(seq: 1, role: "user", content: secret)])
        voice.observe(feed.next())
        voice.observe(feed.add("user", secret))
        voice.observe(feed.reply(secret))
        voice.replay(seq: 3)
        voice.stop()
        voice.setMuted(true)
        voice.observe(feed.reply(secret))
        voice.setMuted(false)
        voice.replay(seq: 4)
        engine.interrupt()
        voice.observe(feed.next { $0.handoff = HandoffInfo(id: "h", from: "mac", to: "ios",
                                                           state: "preparing", fromGen: 1,
                                                           toGen: nil) })
        #expect(engine.seqs == [3, 3, 4])
        #expect(!log.lines.isEmpty)
        for word in secret.split(separator: " ") where word.count > 3 {
            #expect(!log.lines.contains { $0.contains(word) }, "leaked \(word)")
        }
    }

    @Test func tracingChangesNothingStarkSays() {
        func run(_ trace: Trace?) -> ([Utterance], Int, Int?) {
            let store = MemorySpeechStore()
            let engine = FakeSpeechEngine()
            let voice = SpeechCoordinator(engine: engine, store: store, trace: trace)
            let feed = Feed(rows: history(3))
            voice.observe(feed.next())
            voice.observe(feed.reply("a"))
            voice.replay(seq: 2)
            voice.setMuted(true)
            voice.observe(feed.reply("b"))
            voice.setMuted(false)
            voice.observe(feed.add("user", "c"))
            voice.observe(feed.reply("d"))
            engine.interrupt()
            voice.setSuspended(true)
            voice.observe(feed.reply("e"))
            voice.setSuspended(false)
            voice.observe(feed.next())
            voice.observe(feed.reply("f"))
            let key = SpeechLedger.key(agent: "uuid-1", conversation: Wire.conversation,
                                       surface: "mac")
            return (engine.spoken, engine.stops, store.integer(key))
        }
        let plain = run(nil)
        let traced = run(LineLog().trace)
        #expect(plain.0.map(\.id.seq) == traced.0.map(\.id.seq))
        #expect(plain.0.map(\.text) == traced.0.map(\.text))
        #expect(plain.1 == traced.1)
        #expect(plain.2 == traced.2)
    }

    @Test func aSupersededUtteranceStillLogsItsLateCancel() {
        var probes = UtteranceProbes<Int>()
        let t0 = ContinuousClock.now
        let a = Utterance.ID(conversation: "c", seq: 1, token: 1)
        let b = Utterance.ID(conversation: "c", seq: 2, token: 2)
        probes.spoke(1, a, at: t0)
        #expect(probes.started(1, at: t0 + .milliseconds(40)) == .synthStarted(a, ms: 40))
        #expect(probes.started(1, at: t0 + .milliseconds(50)) == nil)
        probes.spoke(2, b, at: t0 + .milliseconds(100))
        // AVFoundation reports the old utterance's cancel after the new one began.
        #expect(probes.started(2, at: t0 + .milliseconds(130)) == .synthStarted(b, ms: 30))
        #expect(probes.ended(1, cancelled: true, at: t0 + .milliseconds(140))
                == .synthCancelled(a, ms: 140, started: true))
        #expect(probes.ended(1, cancelled: true, at: t0 + .milliseconds(150)) == nil)
        #expect(probes.ended(2, cancelled: false, at: t0 + .milliseconds(900))
                == .synthFinished(b, ms: 800, started: true))
        #expect(probes.probes.isEmpty)
    }

    @Test func aSpeakThatNeverStartsIsReportedOnceAndForgottenLater() {
        var probes = UtteranceProbes<Int>()
        let t0 = ContinuousClock.now
        let a = Utterance.ID(conversation: "c", seq: 1, token: 1)
        probes.spoke(1, a, at: t0)
        #expect(probes.noStart(1, a, voice: "v", at: t0 + .seconds(3))
                == .noStart(a, afterMs: 3000, voice: "v"))
        #expect(probes.ended(1, cancelled: false, at: t0 + .seconds(4))
                == .synthFinished(a, ms: 4000, started: false))
        probes.spoke(2, a, at: t0)
        probes.spoke(3, a, at: t0 + UtteranceProbes<Int>.retention)
        #expect(Set(probes.probes.keys) == [3])
    }

    @Test func aWatchdogNeverReportsALaterUtteranceThatReusedItsKey() {
        var probes = UtteranceProbes<Int>()
        let t0 = ContinuousClock.now
        let a = Utterance.ID(conversation: "c", seq: 1, token: 1)
        let b = Utterance.ID(conversation: "c", seq: 2, token: 2)
        probes.spoke(7, a, at: t0)
        _ = probes.ended(7, cancelled: false, at: t0 + .milliseconds(500))
        probes.spoke(7, b, at: t0 + .seconds(1))        // the freed object's identifier, reused
        #expect(probes.noStart(7, a, voice: nil, at: t0 + .seconds(3)) == nil)
        #expect(probes.noStart(7, b, voice: nil, at: t0 + .seconds(4))
                == .noStart(b, afterMs: 3000, voice: nil))
    }

    @Test func everyLineRendersStably() {
        let id = Utterance.ID(conversation: "c", seq: 4, token: 2)
        let cases: [(SpeechDiagnostic, String)] = [
            (.ready(muted: true, voice: "com.apple.voice.Reed"),
             "ready muted=true voice=com.apple.voice.Reed"),
            (.catalog(defaultVoice: nil, offered: 0, installed: 3),
             "catalog default=none offered=0 installed=3"),
            (.armed(agent: "0123456789abcdef", surface: "ios", generation: 7, handled: -1),
             "armed agent=01234567 surface=ios gen=7 handled=-1"),
            (.disarmed(.noIdentity), "disarmed reason=no_identity"),
            (.disarmed(.offline), "disarmed reason=offline"),
            (.accepted(id, origin: .replay, chars: 12), "speak seq=4 tok=2 origin=replay chars=12"),
            (.skippedMuted(seq: 4), "skip seq=4 reason=muted"),
            (.rejected(seq: 4, .disarmed(.handoff)), "replay rejected seq=4 reason=handoff"),
            (.stopped(.audio, playing: id), "stop by=audio seq=4 tok=2"),
            (.suspended(false), "suspended off"),
            (.muted(false), "muted off"),
            (.finished(id), "finished seq=4 tok=2"),
            (.staleFinish(id), "finish stale seq=4 tok=2"),
            (.voice(id, requested: nil, resolved: "v.Reed", name: "Reed", quality: 1,
                    how: .default),
             "voice seq=4 tok=2 requested=default resolved=v.Reed name=Reed quality=1 how=default"),
            (.voice(id, requested: "x", resolved: "v.Reed", name: "Reed", quality: 1,
                    how: .substituted),
             "voice seq=4 tok=2 requested=x resolved=v.Reed name=Reed quality=1 how=substituted"),
            (.voice(id, requested: "gone", resolved: nil, name: nil, quality: nil, how: .none),
             "voice seq=4 tok=2 requested=gone resolved=none name=none quality=none how=none"),
            (.synthStarted(id, ms: 40), "synth start seq=4 tok=2 ms=40"),
            (.synthFinished(id, ms: 900, started: true), "synth finish seq=4 tok=2 ms=900 started=true"),
            (.synthCancelled(id, ms: 3, started: false),
             "synth cancel seq=4 tok=2 ms=3 started=false"),
            (.noStart(id, afterMs: 3001, voice: "v.Reed"),
             "synth no_start seq=4 tok=2 after_ms=3001 voice=v.Reed"),
            (.session(.categoryFailed(code: -50)), "session category failed code=-50"),
            (.session(.activated), "session activate ok"),
            (.session(.activateFailed(code: 560557684)), "session activate failed code=560557684"),
            (.session(.deactivated), "session deactivate ok"),
            (.session(.deactivateFailed(code: 1)), "session deactivate failed code=1"),
            (.session(.interruptionBegan), "session interruption began"),
            (.session(.interruptionEnded), "session interruption ended"),
            (.session(.route(reason: 2)), "session route reason=2"),
            (.session(.mediaReset), "session media_reset"),
            (.voicesChanged(installed: 180), "voices changed installed=180"),
            (.lifecycle(.sleep), "system sleep"),
            (.lifecycle(.wake), "system wake"),
        ]
        for (d, line) in cases { #expect(d.line == line) }
    }
}

/// Speech is presentation: it holds no GatewayClient and must never add a request.
@Suite(.serialized) struct SpeechWritesNothingTests {
    func client(_ rt: FakeRuntime) throws -> GatewayClient {
        let cfg = ClientConfig(socketPath: "/fake", tokenURL: try tokenFile(),
                               backoff: Backoff(base: 0.01, cap: 0.05, jitter: 0))
        return GatewayClient(config: cfg, makeTransport: { _ in rt.connect() })
    }

    func pushReply(_ rt: FakeRuntime, _ text: String) {
        rt.lock.lock()
        let row = Row(seq: rt.rows.count, role: "assistant", content: text)
        rt.rows.append(row)
        let conns = rt.connections
        rt.lock.unlock()
        for c in conns {
            c.push(["type": "event", "kind": "messages", "rev": 3, "data": [
                "conversation_id": Wire.conversation,
                "rows": [["seq": row.seq, "role": row.role, "content": row.content]]]])
        }
    }

    func feed(_ s: ClientState, to voice: SpeechCoordinator) async {
        await MainActor.run { voice.observe(s) }
    }

    @Test func speakingReplayingAndStoppingSendNothingToTheRuntime() async throws {
        let rt = FakeRuntime()
        rt.rows += [Row(seq: 1, role: "user", content: "q"),
                    Row(seq: 2, role: "assistant", content: "old")]
        let c = try client(rt)
        let (voice, engine) = await MainActor.run { speaker() }
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.connection == .live })
        await feed(c.state, to: voice)
        let before = rt.lock.withLock { rt.received }
        pushReply(rt, "new reply")
        #expect(eventually { c.state.rows.count == 4 })
        await feed(c.state, to: voice)
        await MainActor.run {
            #expect(engine.seqs == [3])
            voice.setMuted(true)
            voice.setMuted(false)
            voice.replay(seq: 2)
            voice.stop()
            engine.interrupt()
        }
        pushReply(rt, "another")
        #expect(eventually { c.state.rows.count == 5 })
        await feed(c.state, to: voice)
        await MainActor.run { #expect(engine.seqs == [3, 2, 4]) }
        try await Task.sleep(for: .milliseconds(100))
        let after = rt.lock.withLock { rt.received }
        #expect(after == before)
        #expect(rt.lock.withLock { rt.sends.isEmpty && rt.memoryRequests.isEmpty })
    }
}

@Suite struct VoiceCatalogTests {
    func option(_ name: String, _ lang: String = "en-US", q: Int = 1) -> VoiceOption {
        VoiceOption(id: "id.\(lang).\(name).\(q)", name: name, language: lang, quality: q)
    }

    @Test func theDefaultPrefersAYouthfulVoiceOverQualityAlone() {
        let opts = [option("Ava", q: 3), option("Grandpa"), option("Eddy"), option("Evan", q: 2)]
        #expect(VoiceCatalog.pickDefault(opts, language: "en-US")?.name == "Evan")
    }

    @Test func theDefaultPicksTheBestQualityOfAPreferredName() {
        let opts = [option("Evan"), option("Evan", q: 3), option("Evan", "en-GB", q: 3)]
        #expect(VoiceCatalog.pickDefault(opts, language: "en-US") == option("Evan", q: 3))
    }

    @Test func withoutAPreferredVoiceTheBestOfferedVoiceWins() {
        let opts = [option("Kathy"), option("Samantha", q: 2)]
        #expect(VoiceCatalog.pickDefault(opts, language: "en-US")?.name == "Samantha")
        #expect(VoiceCatalog.pickDefault([], language: "en-US") == nil)
    }

    @MainActor @Test func resolvingAVoiceMatchesWhatIsSpokenAndSaysHow() {
        let bogus = VoiceCatalog.resolve("dev.gameshark.no.such.voice")
        #expect(bogus.1 != .requested)
        #expect(bogus.0?.identifier == VoiceCatalog.voice("dev.gameshark.no.such.voice")?.identifier)
        #expect(VoiceCatalog.resolve(nil).1 != .requested)
        if let installed = AVSpeechSynthesisVoice.speechVoices().first {
            let r = VoiceCatalog.resolve(installed.identifier)
            #expect(r.1 == .requested)
            #expect(r.0?.identifier == installed.identifier)
        }
    }

    @Test func elderAndNoveltyNamesAreNeverTheDefault() {
        let opts = [option("Grandma", q: 3), option("Grandpa", q: 3), option("Kathy")]
        #expect(VoiceCatalog.pickDefault(opts, language: "en-US")?.name == "Kathy")
    }
}
