import Foundation
import Testing
@testable import StarkKit

@Suite struct MemoryCommandTests {
    @Test func onlyAnExplicitCommandSavesMemory() {
        #expect(MemoryCommand.parse("/remember my sister is Ana") == .remember("my sister is Ana"))
        #expect(MemoryCommand.parse("  /REMEMBER   coffee, no sugar ") == .remember("coffee, no sugar"))
        #expect(MemoryCommand.parse("/remember") == .show)
        #expect(MemoryCommand.parse("/remember   ") == .show)
        #expect(MemoryCommand.parse("/memories") == .show)
        #expect(MemoryCommand.parse("please remember my sister is Ana") == nil)
        #expect(MemoryCommand.parse("remember: my sister is Ana") == nil)
        #expect(MemoryCommand.parse("/rememberable things") == nil)
        #expect(MemoryCommand.parse("hello") == nil)
    }

    @Test func forgetNoticeIsHonestAboutHistory() {
        #expect(MemoryText.forgotten(historyMentions: 0) == "Removed from saved memory.")
        #expect(MemoryText.forgotten(historyMentions: 1).contains("1 earlier chat message still"))
        #expect(MemoryText.forgotten(historyMentions: 3).contains("3 earlier chat messages"))
    }

    @Test func snapshotDecodesWithAndWithoutMemory() throws {
        let base = """
        {"server":"s","identity":{"agent_id":"stark","uuid":"u"},"you":"mac",
         "presence":{"surface":"mac","home":"mac","generation":1},"responsibilities":[],
         "conversation":{"id":"stark:main","head_seq":0},"activity":{"working":false}
        """
        let old = try Wire.decode(Snapshot.self, from: Data((base + "}").utf8))
        #expect(old.features == nil && old.memory == nil)
        let new = try Wire.decode(Snapshot.self, from: Data((base +
            #","features":["memory"],"memory":{"count":2,"inject":false}}"#).utf8))
        #expect(new.features == ["memory"])
        #expect(new.memory == MemorySummary(count: 2, inject: false))
    }
}

@Suite(.serialized) struct MemoryClientTests {
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

    @Test func listsMemoriesOnConnectWhenTheRuntimeServesThem() throws {
        let rt = FakeRuntime()
        rt.memoryFeature = true
        rt.memories = [["id": "mem-1", "content": "likes tea", "revision": 1, "scope": "global",
                        "state": "active", "source_surface": "mac", "created": 1.0,
                        "updated": 1.0]]
        let c = try live(rt)
        defer { c.stop() }
        #expect(eventually { c.state.memories.map(\.content) == ["likes tea"] })
        #expect(c.state.memoryAvailable && c.state.canWriteMemory)
    }

    @Test func anOlderRuntimeHidesMemory() throws {
        let rt = FakeRuntime()
        let c = try live(rt)
        defer { c.stop() }
        #expect(!c.state.memoryAvailable && !c.state.canWriteMemory && !c.state.canForgetMemory)
        c.remember("waits for a runtime that serves memory")
        Thread.sleep(forTimeInterval: 0.1)
        #expect(rt.lock.withLock { rt.memoryRequests.isEmpty })
        #expect(c.state.memoryOps.first?.status == .pending)
    }

    @Test func rememberCorrectForgetRoundTrip() throws {
        let rt = FakeRuntime()
        rt.memoryFeature = true
        let c = try live(rt)
        defer { c.stop() }
        c.remember("lives in Boston")
        #expect(eventually { c.state.memories.first?.content == "lives in Boston" })
        #expect(c.state.memoryOps.isEmpty)
        let m = c.state.memories[0]
        c.correctMemory(id: m.id, revision: m.revision, text: "lives in Atlanta")
        #expect(eventually { c.state.memories.first?.revision == 2 })
        #expect(c.state.memories.map(\.content) == ["lives in Atlanta"])
        c.forgetMemory(id: m.id)
        #expect(eventually { c.state.memories.isEmpty })
        #expect(c.state.memoryNotice == MemoryText.forgotten(historyMentions: 1))
    }

    @Test func aLostAnswerIsResentUnderTheSameOpAndLandsOnce() throws {
        let rt = FakeRuntime()
        rt.memoryFeature = true
        let c = try live(rt)
        defer { c.stop() }
        rt.lock.withLock { rt.dropNextMemoryReply = true }
        let op = c.remember("survives a dropped connection")
        #expect(eventually { c.state.memories.count == 1 && c.state.memoryOps.isEmpty })
        rt.lock.withLock {
            #expect(rt.memoryRequests == [op, op])
            #expect(rt.memories.count == 1)
            #expect(rt.connections.count == 2)
        }
    }

    @Test func stalePresenceRefusesRememberButForgetStillWorks() throws {
        let rt = FakeRuntime()
        rt.memoryFeature = true
        rt.memories = [["id": "mem-1", "content": "x", "revision": 1, "scope": "global",
                        "state": "active", "source_surface": "mac", "created": 1.0,
                        "updated": 1.0]]
        rt.surface = "pocket-a1b2c3"                   // Stark is on the phone
        let c = try live(rt)
        defer { c.stop() }
        #expect(!c.state.canWriteMemory && c.state.canForgetMemory)
        let op = c.remember("not while Stark is away")
        #expect(eventually { c.state.memoryOps.first?.status == .rejected(code: "stale_presence") })
        #expect(c.state.memoryNotice == MemoryText.refused("stale_presence"))
        c.discardMemoryOp(op)
        #expect(eventually { c.state.memoryOps.isEmpty && c.state.memoryNotice == nil })
        c.forgetMemory(id: "mem-1")
        #expect(eventually { c.state.memories.isEmpty })
        #expect(rt.lock.withLock { rt.memories.isEmpty })
    }

    @Test func aConflictRefreshesTheList() throws {
        let rt = FakeRuntime()
        rt.memoryFeature = true
        rt.memories = [["id": "mem-1", "content": "v2", "revision": 2, "scope": "global",
                        "state": "active", "source_surface": "mac", "created": 1.0,
                        "updated": 1.0]]
        let c = try live(rt)
        defer { c.stop() }
        c.correctMemory(id: "mem-1", revision: 1, text: "from a stale screen")
        #expect(eventually { c.state.memoryOps.first?.status == .rejected(code: "conflict") })
        #expect(c.state.memories.first?.content == "v2")
        #expect(rt.lock.withLock { rt.received.filter { $0 == "memory_list" }.count } >= 2)
    }

    @Test func forgettingSomethingAlreadyGoneSettlesQuietly() throws {
        let rt = FakeRuntime()
        rt.memoryFeature = true
        let c = try live(rt)
        defer { c.stop() }
        c.forgetMemory(id: "mem-gone")
        #expect(eventually { c.state.memoryOps.isEmpty })
        #expect(c.state.memoryNotice == nil)
    }

    @Test func anotherRuntimeNeverReceivesPendingMemoryWrites() throws {
        let rt = FakeRuntime()                         // serves no memory yet: ops wait
        let c = try live(rt)
        defer { c.stop() }
        c.remember("a private fact for uuid-1")
        #expect(eventually { c.state.memoryOps.count == 1 })
        rt.lock.withLock {
            rt.uuid = "uuid-other"                    // another database answers next
            rt.memoryFeature = true
        }
        c.reconnectNow()
        #expect(eventually { c.state.identity?.uuid == "uuid-other" && c.state.connection == .live })
        Thread.sleep(forTimeInterval: 0.1)
        #expect(c.state.memoryOps.isEmpty)
        #expect(rt.lock.withLock { rt.memoryRequests.isEmpty })
    }

    @Test func aFencedOrDrainingRefusalStaysPendingAndResendsUnderTheSameOp() throws {
        for code in ["fenced", "draining"] {
            let rt = FakeRuntime()
            rt.memoryFeature = true
            rt.memoryErrorOnce = code
            let c = try live(rt)
            let op = c.remember("survives an ownership change (\(code))")
            #expect(eventually { rt.lock.withLock { rt.memoryRequests.count } == 1 })
            Thread.sleep(forTimeInterval: 0.05)
            #expect(c.state.memoryOps.first?.status == .pending)
            #expect(c.state.memoryNotice == nil)
            c.reconnectNow()                            // the next owner takes the resend
            #expect(eventually { c.state.memories.count == 1 && c.state.memoryOps.isEmpty })
            #expect(rt.lock.withLock { rt.memoryRequests } == [op, op])
            c.stop()
        }
    }

    @Test func supersededSettlesQuietlyAndDuplicateExplains() throws {
        let rt = FakeRuntime()
        rt.memoryFeature = true
        let c = try live(rt)
        defer { c.stop() }
        rt.lock.withLock { rt.memoryErrorOnce = "superseded" }
        c.remember("landed earlier")
        #expect(eventually { c.state.memoryOps.isEmpty })
        #expect(c.state.memoryNotice == nil)
        #expect(eventually { rt.lock.withLock { rt.received.filter { $0 == "memory_list" }.count } >= 2 })
        rt.lock.withLock { rt.memoryErrorOnce = "duplicate" }
        c.correctMemory(id: "mem-1", revision: 1, text: "same as another")
        #expect(eventually { c.state.memoryOps.first?.status == .rejected(code: "duplicate") })
        #expect(c.state.memoryNotice == MemoryText.refused("duplicate"))
    }

    @Test func aPushReplacesTheList() throws {
        let rt = FakeRuntime()
        rt.memoryFeature = true
        let c = try live(rt)
        defer { c.stop() }
        rt.lock.withLock {
            rt.memories = [["id": "mem-9", "content": "saved on the phone", "revision": 1,
                            "scope": "global", "state": "active",
                            "source_surface": "pocket-a1b2c3", "source_device_id": "d1",
                            "created": 1.0, "updated": 1.0]]
        }
        rt.pushMemories()
        #expect(eventually { c.state.memories.first?.sourceSurface == "pocket-a1b2c3" })
    }
}
