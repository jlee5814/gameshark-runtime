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

    @Test func elderAndNoveltyNamesAreNeverTheDefault() {
        let opts = [option("Grandma", q: 3), option("Grandpa", q: 3), option("Kathy")]
        #expect(VoiceCatalog.pickDefault(opts, language: "en-US")?.name == "Kathy")
    }
}
