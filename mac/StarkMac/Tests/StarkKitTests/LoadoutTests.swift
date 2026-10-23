import Foundation
import Testing
@testable import StarkKit

@Suite struct LoadoutWireTests {
    static let base = """
    {"server":"s","identity":{"agent_id":"stark","uuid":"u"},"you":"mac",
     "presence":{"surface":"mac","home":"mac","generation":1},"responsibilities":[],
     "conversation":{"id":"stark:main","head_seq":0},"activity":{"working":false}
    """

    @Test func snapshotDecodesWithAndWithoutLoadout() throws {
        let old = try Wire.decode(Snapshot.self, from: Data((Self.base + "}").utf8))
        #expect(old.loadout == nil)
        let new = try Wire.decode(Snapshot.self, from: Data((Self.base + """
            ,"features":["loadout"],"loadout":{"effective":"gym","inferred":"default",
            "override":"gym","inferred_seq":null}}
            """).utf8))
        #expect(new.loadout == LoadoutState(effective: .gym, inferred: .standard, pinned: .gym))
    }

    @Test func anUnknownLoadoutDrawsTheStandardSprite() throws {
        let s = try Wire.decode(LoadoutState.self, from: Data("""
            {"effective":"tuxedo","inferred":"gym","override":"tuxedo","inferred_seq":4}
            """.utf8))
        #expect(s == LoadoutState(effective: .standard, inferred: .gym, pinned: .standard,
                                  inferredSeq: 4))
    }

    @Test func onlyExactCommandsSetTheLoadout() {
        #expect(LoadoutCommand.parse("/gym") == .pin(.gym))
        #expect(LoadoutCommand.parse("  /GYM ") == .pin(.gym))
        #expect(LoadoutCommand.parse("/default") == .pin(.standard))
        #expect(LoadoutCommand.parse("/auto") == .auto)
        #expect(LoadoutCommand.parse("/auto")?.requested == nil)
        #expect(LoadoutCommand.parse("/gym")?.requested == .gym)
        #expect(LoadoutCommand.parse("I'm at the gym") == nil)    // the runtime infers that
        #expect(LoadoutCommand.parse("/gym time") == nil)
        #expect(LoadoutCommand.parse("/gymnastics") == nil)
        #expect(LoadoutCommand.parse("gym") == nil)
    }

    @Test func atlasFilesMatchTheCommittedAssets() {
        #expect(Loadout.standard.atlasFile == "stark-atlas.png")
        #expect(Loadout.gym.atlasFile == "stark-atlas-gym.png")
    }
}

@Suite struct LoadoutAtlasTests {
    @Test func theGymAtlasSharesTheManifestLayout() throws {
        let m = try manifest()
        let std = try #require(AtlasCrop.frames(at: assets.appendingPathComponent(m.atlas.file),
                                                manifest: m))
        let gym = try #require(AtlasCrop.frames(
            at: assets.appendingPathComponent(Loadout.gym.atlasFile), manifest: m))
        #expect(std.count == 74 && gym.count == 74)
        for (a, b) in zip(std, gym) {
            #expect(a.width == b.width && a.height == b.height)
        }
        let frames = LoadoutFrames(standard: std, gym: gym)
        #expect(frames.frames(for: .gym)[0] !== frames.frames(for: .standard)[0])
    }

    @Test func aMissingOrMismatchedGymAtlasDrawsStandard() throws {
        let m = try manifest()
        let std = try #require(AtlasCrop.frames(at: assets.appendingPathComponent(m.atlas.file),
                                                manifest: m))
        #expect(AtlasCrop.frames(at: assets.appendingPathComponent("nope.png"), manifest: m) == nil)
        #expect(AtlasCrop.frames(at: assets.appendingPathComponent("preview.png"),
                                 manifest: m) == nil)
        let short = LoadoutFrames(standard: std, gym: Array(std.prefix(3)))
        #expect(short.gym == nil && short.frames(for: .gym).count == 74)
    }
}

@Suite(.serialized) struct LoadoutClientTests {
    static var gymRow: [String: Any] {
        ["effective": "gym", "inferred": "gym", "override": NSNull(), "inferred_seq": 3]
    }

    func client(_ rt: FakeRuntime) throws -> GatewayClient {
        let cfg = ClientConfig(socketPath: "/fake", tokenURL: try tokenFile(),
                               backoff: Backoff(base: 0.01, cap: 0.05, jitter: 0))
        return GatewayClient(config: cfg, makeTransport: { _ in rt.connect() })
    }

    func live(_ rt: FakeRuntime) throws -> GatewayClient {
        let c = try client(rt)
        c.start()
        #expect(eventually { c.state.connection == .live })
        return c
    }

    @Test func theSnapshotSetsTheLoadout() throws {
        let rt = FakeRuntime()
        rt.loadoutFeature = true
        rt.loadout = Self.gymRow
        let c = try live(rt)
        defer { c.stop() }
        #expect(c.state.loadoutAvailable)
        #expect(c.state.loadout.effective == .gym && c.state.shownLoadout == .gym)
        #expect(c.state.loadout.inferredSeq == 3)
    }

    @Test func anOlderRuntimeWearsTheStandardSpriteAndHoldsPins() throws {
        let rt = FakeRuntime()
        let c = try live(rt)
        defer { c.stop() }
        #expect(!c.state.loadoutAvailable && c.state.shownLoadout == .standard)
        c.setLoadout(.gym)
        Thread.sleep(forTimeInterval: 0.1)
        #expect(rt.lock.withLock { rt.loadoutRequests.isEmpty })
    }

    @Test func aPushFromAnotherSurfaceSwitchesThisOne() throws {
        let rt = FakeRuntime()
        rt.loadoutFeature = true
        let c = try live(rt)
        defer { c.stop() }
        #expect(c.state.shownLoadout == .standard)
        rt.pushLoadout(Self.gymRow)
        #expect(eventually { c.state.shownLoadout == .gym })
        rt.pushLoadout(["effective": "default", "inferred": "default", "override": NSNull()])
        #expect(eventually { c.state.shownLoadout == .standard })
    }

    @Test func aPinShowsAtOnceAndTheRuntimeConfirmsIt() throws {
        let rt = FakeRuntime()
        rt.loadoutFeature = true
        let c = try live(rt)
        defer { c.stop() }
        c.setLoadout(.gym)
        #expect(eventually { c.state.loadout.pinned == .gym && c.state.loadoutOp == nil })
        #expect(c.state.shownLoadout == .gym)
        c.setLoadout(nil)
        #expect(eventually { c.state.loadout.pinned == nil && c.state.loadoutOp == nil })
        #expect(c.state.shownLoadout == .standard)
        let sent = rt.lock.withLock { rt.loadoutRequests }
        #expect(sent.count == 2)
        #expect(sent[0]["override"] as? String == "gym")
        #expect(sent[1]["override"] is NSNull)
        #expect(Set(sent.compactMap { $0["op_id"] as? String }).count == 2)
        // Costume only: no chat row, memory write, or retry went out.
        let other = rt.lock.withLock { rt.received.filter { !["snapshot", "sync", "loadout_set"]
            .contains($0) } }
        #expect(other.isEmpty)
    }

    @Test func aPinMadeOfflineGoesOutOnReconnect() throws {
        let rt = FakeRuntime()
        rt.loadoutFeature = true
        let c = try live(rt)
        defer { c.stop() }
        rt.lock.withLock { rt.blackhole = true }
        rt.lock.withLock { rt.connections.last }?.hangUp()
        #expect(eventually { c.state.connection != .live })
        c.setLoadout(.gym)
        #expect(eventually { c.state.shownLoadout == .gym })      // shown while pending
        rt.lock.withLock { rt.blackhole = false }
        c.reconnectNow()
        #expect(eventually { c.state.loadoutOp == nil && c.state.loadout.pinned == .gym })
        #expect(rt.lock.withLock { rt.loadoutRequests.count } == 1)
    }

    @Test func aDroppedSessionKeepsTheCapUntilTheNextSnapshot() throws {
        let rt = FakeRuntime()
        rt.loadoutFeature = true
        rt.loadout = Self.gymRow
        let c = try live(rt)
        defer { c.stop() }
        rt.lock.withLock { rt.blackhole = true }
        rt.lock.withLock { rt.connections.last }?.hangUp()
        #expect(eventually { c.state.connection != .live })
        #expect(c.state.shownLoadout == .gym)
        // Changed while this surface was away: the reconnect snapshot carries it.
        rt.lock.withLock {
            rt.loadout = ["effective": "default", "inferred": "default", "override": NSNull()]
            rt.blackhole = false
        }
        c.reconnectNow()
        #expect(eventually { c.state.connection == .live && c.state.shownLoadout == .standard })
    }

    @Test func aRefusedPinRevertsToTheRuntimeValue() throws {
        let rt = FakeRuntime()
        rt.loadoutFeature = true
        let c = try live(rt)
        defer { c.stop() }
        rt.lock.withLock { rt.loadoutErrorOnce = "invalid" }
        c.setLoadout(.gym)
        #expect(eventually { c.state.loadoutOp == nil && c.state.lastError == "loadout: invalid" })
        #expect(c.state.shownLoadout == .standard)
    }

    @Test func aFencedRefusalStaysPendingAndResends() throws {
        let rt = FakeRuntime()
        rt.loadoutFeature = true
        let c = try live(rt)
        defer { c.stop() }
        rt.lock.withLock { rt.loadoutErrorOnce = "fenced" }
        c.setLoadout(.gym)
        #expect(eventually { rt.lock.withLock { rt.loadoutRequests.count } == 1 })
        #expect(c.state.loadoutOp?.pin == .gym)
        c.reconnectNow()
        #expect(eventually { c.state.loadoutOp == nil && c.state.loadout.pinned == .gym })
        let ops = rt.lock.withLock { rt.loadoutRequests.compactMap { $0["op_id"] as? String } }
        #expect(ops.count == 2 && ops[0] == ops[1])                 // the same op, resent
    }

    @Test func anotherRuntimeResetsTheLoadout() throws {
        let rt = FakeRuntime()
        rt.loadoutFeature = true
        rt.loadout = Self.gymRow
        let c = try live(rt)
        defer { c.stop() }
        #expect(c.state.shownLoadout == .gym)
        rt.lock.withLock {
            rt.uuid = "uuid-2"
            rt.loadout = nil
        }
        c.reconnectNow()
        #expect(eventually { c.state.identity?.uuid == "uuid-2" })
        #expect(eventually { c.state.shownLoadout == .standard })
    }
}
