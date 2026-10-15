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
