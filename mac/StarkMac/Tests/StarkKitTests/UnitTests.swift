import Foundation
import Testing
@testable import StarkKit

@Suite struct FrameTests {
    @Test func roundTripsAcrossArbitraryChunking() throws {
        let a = try Frame.encode(json: ["type": "ping", "id": 1])
        let b = try Frame.encode(json: ["type": "snapshot", "id": 2])
        let stream = a + b
        var reader = FrameReader()
        var bodies: [Data] = []
        for byte in stream {
            reader.feed(Data([byte]))
            while let body = try reader.next() { bodies.append(body) }
        }
        #expect(bodies.count == 2)
        let head = try Wire.decode(Head.self, from: bodies[1])
        #expect(head.type == "snapshot" && head.id == 2)
    }

    @Test func refusesOversizeAndEmptyFrames() throws {
        #expect(throws: Frame.Failure.tooLarge(Frame.maxBytes + 1)) {
            try Frame.encode(Data(count: Frame.maxBytes + 1))
        }
        var reader = FrameReader()
        var n = UInt32(Frame.maxBytes + 1).bigEndian
        reader.feed(Data(bytes: &n, count: 4))
        #expect(throws: Frame.Failure.tooLarge(Frame.maxBytes + 1)) { try reader.next() }
        var empty = FrameReader()
        empty.feed(Data(count: 4))
        #expect(throws: Frame.Failure.empty) { try empty.next() }
    }

    @Test func acceptsExactlyTheCap() throws {
        let body = Data(repeating: 0x20, count: Frame.maxBytes)
        var reader = FrameReader()
        reader.feed(try Frame.encode(body))
        #expect(try reader.next()?.count == Frame.maxBytes)
    }
}

@Suite struct AuthTests {
    // Shared with prototype/tests/test_gateway.py: both clients must agree byte for byte.
    @Test func matchesThePythonKnownAnswer() {
        let token = Data((0..<32).map { UInt8($0) })
        let nonce = Data(repeating: 0xAA, count: 32)
        #expect(Auth.sign(token: token, nonce: nonce, surface: "mac", proto: 1)
            == "9229d616a443a3e82e05a8c81f3b383623e7879c3a33a92a6947729dd42758c9")
    }

    @Test func bindsSurfaceAndVersion() {
        let token = Data(repeating: 1, count: 32)
        let nonce = Data(repeating: 2, count: 32)
        let mac = Auth.sign(token: token, nonce: nonce, surface: "mac", proto: 1)
        #expect(mac != Auth.sign(token: token, nonce: nonce, surface: "phone", proto: 1))
        #expect(mac != Auth.sign(token: token, nonce: nonce, surface: "mac", proto: 2))
    }

    @Test func refusesATokenOthersCanRead() throws {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("tok-\(UUID().uuidString)")
        try Data(String(repeating: "ab", count: 32).utf8).write(to: url)
        defer { try? FileManager.default.removeItem(at: url) }
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: url.path)
        #expect(throws: Auth.TokenFailure.tooOpen(url.path)) { try Auth.loadToken(at: url) }
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        #expect(try Auth.loadToken(at: url).count == 32)
    }

    @Test func hexRoundTrips() {
        let d = Data([0, 1, 0xfe, 0xff])
        #expect(Data(hex: d.hex) == d)
        #expect(Data(hex: "abc") == nil)
        #expect(Data(hex: "zz") == nil)
    }
}

@Suite struct MirrorTests {
    func row(_ seq: Int) -> Row { Row(seq: seq, role: seq % 2 == 0 ? "assistant" : "user", content: "r\(seq)") }

    @Test func mergeIsIdempotent() {
        var m = ConversationMirror()
        #expect(m.merge([row(0), row(1)]) == 2)
        #expect(m.merge([row(1), row(0)]) == 0)
        #expect(m.ordered.map(\.seq) == [0, 1])
    }

    @Test func outOfOrderRowsRenderInOrder() {
        var m = ConversationMirror()
        m.merge([row(2), row(0), row(1)])
        #expect(m.ordered.map(\.seq) == [0, 1, 2])
        #expect(m.contiguousHead == 2 && !m.hasGap)
    }

    @Test func gapHoldsTheContiguousHeadForResync() {
        var m = ConversationMirror()
        m.merge([row(0), row(1), row(4)])
        #expect(m.hasGap && m.contiguousHead == 1 && m.headSeq == 4)
        m.merge([row(2), row(3)])
        #expect(!m.hasGap && m.contiguousHead == 4)
    }

    @Test func resetForgetsEverything() {
        var m = ConversationMirror()
        m.merge([row(0)])
        m.reset()
        #expect(m.ordered.isEmpty && m.contiguousHead == -1)
    }
}

@Suite struct BackoffTests {
    @Test func doublesToTheCapAndResets() {
        var b = Backoff(base: 0.25, cap: 5, jitter: 0)
        let delays = (0..<8).map { _ in b.next(unit: 0.5) }
        #expect(delays == [0.25, 0.5, 1, 2, 4, 5, 5, 5])
        b.reset()
        #expect(b.next(unit: 0.5) == 0.25)
    }

    @Test func jitterStaysInBand() {
        var b = Backoff(base: 1, cap: 1, jitter: 0.2)
        #expect(abs(b.next(unit: 0) - 0.8) < 1e-9)
        #expect(abs(b.next(unit: 1) - 1.2) < 1e-9)
    }
}

@Suite struct AvatarStateTests {
    func state(live: Bool = true, surface: String = "mac", working: Bool = false,
               rows: [Row] = []) -> ClientState {
        var s = ClientState(surface: "mac")
        s.connection = live ? .live : .disconnected("gone")
        s.presence = PresenceInfo(surface: surface, home: "mac", generation: 1)
        s.working = working
        s.rows = rows
        return s
    }

    let reply = [Row(seq: 1, role: "user", content: "hi"), Row(seq: 2, role: "assistant", content: "yo")]

    @Test func precedence() {
        #expect(AvatarState.derive(state(live: false, working: true), listening: true, viewedSeq: -1) == .disconnected)
        #expect(AvatarState.derive(state(surface: "phone", working: true), listening: true, viewedSeq: -1) == .ghost)
        #expect(AvatarState.derive(state(working: true), listening: true, viewedSeq: -1) == .working)
        #expect(AvatarState.derive(state(rows: reply), listening: true, viewedSeq: -1) == .listening)
        #expect(AvatarState.derive(state(rows: reply), listening: false, viewedSeq: 1) == .notification)
        #expect(AvatarState.derive(state(rows: reply), listening: false, viewedSeq: 2) == .idle)
        #expect(AvatarState.derive(state(), listening: false, viewedSeq: -1) == .idle)
    }

    @Test func aReplyStillOnItsWayIsWorkAndAFailedOneIsNot() {
        for st in ["queued", "thinking", "waiting"] {
            var s = state()
            s.activity = Activity(reply: ReplyState(seq: 1, state: st))
            #expect(AvatarState.derive(s, listening: false, viewedSeq: -1) == .working)
        }
        var s = state()
        s.activity = Activity(reply: ReplyState(seq: 1, state: "failed", error: "Timeout"))
        #expect(AvatarState.derive(s, listening: false, viewedSeq: -1) == .idle)
    }
}

/// The same table as prototype/tests/test_stark_paths.py: socket from STARK_SOCKET, else
/// gateway/stark.sock beside the default database; token from STARK_TOKEN, else
/// gateway.token beside the socket.
@Suite struct ConfigPathTests {
    let home = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".gameshark")

    @Test func defaultsToTheDedicatedGatewayDirectory() {
        let c = ClientConfig.fromEnvironment([:])
        #expect(c.socketPath == home.appendingPathComponent("gateway/stark.sock").path)
        #expect(c.tokenURL.path == home.appendingPathComponent("gateway/gateway.token").path)
    }

    @Test func customSocketCarriesItsToken() {
        let c = ClientConfig.fromEnvironment(["STARK_SOCKET": "/e/stark.sock"])
        #expect(c.socketPath == "/e/stark.sock")
        #expect(c.tokenURL.path == "/e/gateway.token")
    }

    @Test func tokenOverrideWins() {
        let c = ClientConfig.fromEnvironment(["STARK_TOKEN": "/t/secret"])
        #expect(c.socketPath == home.appendingPathComponent("gateway/stark.sock").path)
        #expect(c.tokenURL.path == "/t/secret")
    }
}

@Suite struct DraftContinuityTests {
    let carried = CarriedDraft(handoffId: "h1", text: "from my pocket")

    @Test func anEmptyComposerTakesTheDraftWithoutSending() {
        #expect(DraftContinuity.arrival(of: carried, composer: "") == .restore("from my pocket"))
        #expect(DraftContinuity.arrival(of: carried, composer: "  \n") == .restore("from my pocket"))
    }

    @Test func theSameTextIsAlreadyHere() {
        #expect(DraftContinuity.arrival(of: carried, composer: "from my pocket") == .alreadyHere)
    }

    @Test func differentTextIsAConflictNeverAnOverwrite() {
        #expect(DraftContinuity.arrival(of: carried, composer: "my own words") == .conflict)
    }

    func state(holder: String, handoff: HandoffInfo? = nil) -> ClientState {
        var s = ClientState(surface: "mac")
        s.presence = PresenceInfo(surface: holder, home: "mac", generation: 3)
        s.handoff = handoff
        return s
    }

    @Test func theSourceClearsOnlyTextThatLeftWithStark() {
        let sent = CarriedDraft(handoffId: "h1", text: "half typed")
        let open = HandoffInfo(id: "h1", from: "mac", to: "phone", state: "committed",
                               fromGen: 2, toGen: 3)
        #expect(DraftContinuity.departure(of: sent, composer: "half typed",
                                          state: state(holder: "phone", handoff: open)) == .pending)
        #expect(DraftContinuity.departure(of: sent, composer: "half typed",
                                          state: state(holder: "phone")) == .clearComposer)
        #expect(DraftContinuity.departure(of: sent, composer: "half typed and more",
                                          state: state(holder: "phone")) == .keepComposer)
        #expect(DraftContinuity.departure(of: sent, composer: "half typed",
                                          state: state(holder: "mac")) == .keepComposer)  // reverted
    }
}

extension DraftContinuityTests {
    @Test func followRestoresIntoAnEmptyComposerAndConfirms() {
        var s = state(holder: "mac")
        s.incomingDraft = carried
        let steps = DraftContinuity.follow(s, composer: "", offered: nil)
        #expect(steps.composer == "from my pocket" && steps.confirm == "h1" && steps.offer == nil)
    }

    @Test func followOffersAConflictAndWaitsForTheUser() {
        var s = state(holder: "mac")
        s.incomingDraft = carried
        let first = DraftContinuity.follow(s, composer: "mine", offered: nil)
        #expect(first.offer == carried && first.composer == nil && first.confirm == nil)
        let again = DraftContinuity.follow(s, composer: "mine", offered: carried)
        #expect(again == DraftContinuity.Steps())
        s.incomingDraft = nil
        #expect(DraftContinuity.follow(s, composer: "mine", offered: carried).withdrawOffer)
    }

    @Test func followClearsTheSourceThenTakesARoundTripBackWithoutConflict() {
        var s = state(holder: "phone")
        s.sentDraft = CarriedDraft(handoffId: "h1", text: "half typed")
        let left = DraftContinuity.follow(s, composer: "half typed", offered: nil)
        #expect(left.composer == "" && left.clearSent == "h1")
        s = state(holder: "mac")
        s.incomingDraft = CarriedDraft(handoffId: "h2", text: "half typed, finished on the phone")
        let back = DraftContinuity.follow(s, composer: "", offered: nil)
        #expect(back.composer == "half typed, finished on the phone" && back.confirm == "h2")
    }
}

/// Phase 4.1: the head reply's state and the model behind it, as the runtime reports them.
@Suite struct ReplyStateTests {
    func activity(_ json: String) throws -> Activity {
        try Wire.decode(Activity.self, from: Data(json.utf8))
    }

    @Test func decodesEachStateAndItsReason() throws {
        let a = try activity(#"{"working": false, "reply": {"seq": 3, "retry": 1, "state": "waiting", "error": "ModelUnavailable", "retry_at": 1792190000.5}}"#)
        #expect(a.reply == ReplyState(seq: 3, retry: 1, state: "waiting", error: "ModelUnavailable",
                                      retryAt: 1792190000.5))
        #expect(a.reply?.caption == "Waiting for the model: the model server is unreachable")
        let f = try activity(#"{"working": false, "reply": {"seq": 3, "state": "failed", "error": "AttemptsExhausted"}}"#)
        #expect(f.reply?.failed == true)
        #expect(f.reply?.caption == "No reply: the model failed three times")
    }

    @Test func absentOrNullMeansAnsweredOrAnOlderRuntime() throws {
        #expect(try activity(#"{"working": false}"#).reply == nil)
        #expect(try activity(#"{"working": false, "reply": null}"#).reply == nil)
    }

    @Test func captionsNameEveryStateWithoutInventingAnAnswer() {
        #expect(ReplyState(seq: 1, state: "queued").caption == "Stark has your message")
        #expect(ReplyState(seq: 1, state: "thinking").caption == "Stark is thinking…")
        #expect(ReplyState(seq: 1, state: "failed", error: "Timeout").caption
                == "No reply: the model took too long")
        #expect(ReplyState(seq: 1, state: "failed", error: "Strange").caption == "No reply: Strange")
        #expect(ReplyState(seq: 1, state: "failed").caption == "No reply")
    }

    @Test func snapshotCarriesTheModel() throws {
        let json = #"{"provider": "local", "model": "mlx-community/Qwen3-4B-4bit", "locality": "local", "fake": false}"#
        let m = try Wire.decode(ModelInfo.self, from: Data(json.utf8))
        #expect(m == ModelInfo(provider: "local", model: "mlx-community/Qwen3-4B-4bit",
                               locality: "local", fake: false))
    }

    @Test func onlyTheActiveLiveSurfaceMayRetryAFailedReply() {
        var s = ClientState(surface: "mac")
        s.connection = .live
        s.presence = PresenceInfo(surface: "mac", home: "mac", generation: 1)
        s.activity = Activity(reply: ReplyState(seq: 1, state: "failed", error: "Timeout"))
        #expect(s.canRetryReply)
        s.presence = PresenceInfo(surface: "phone", home: "mac", generation: 2)
        #expect(!s.canRetryReply)
        s.presence = PresenceInfo(surface: "mac", home: "mac", generation: 3)
        s.connection = .disconnected("gone")
        #expect(!s.canRetryReply)
        s.connection = .live
        s.activity = Activity(reply: ReplyState(seq: 1, state: "thinking"))
        #expect(!s.canRetryReply)
    }
}
