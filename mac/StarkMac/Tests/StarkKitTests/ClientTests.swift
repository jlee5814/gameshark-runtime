import Foundation
import Testing
@testable import StarkKit

/// An in memory runtime speaking protocol v1: rows, dedup by client_msg_id, presence
/// generation fencing. Each connection is one FakeTransport.
final class FakeRuntime: @unchecked Sendable {
    let lock = NSLock()
    var rows: [Row] = [Row(seq: 0, role: "system", content: "sys")]
    var landed: [String: Int] = [:]
    var generation = 1
    var surface = "mac"
    var uuid = "uuid-1"
    var connections: [FakeTransport] = []
    var sends: [String] = []
    /// When set, the next send lands but its reply is lost and the connection drops.
    var dropAfterNextSend = false
    /// The open jack the snapshot reports, in wire form.
    var handoff: [String: Any]?
    /// Every ready_out and ack_in received, in order.
    var handoffCalls: [String] = []
    /// When set, the next ready_out or ack_in is lost on the way: the connection drops
    /// before the runtime applies it.
    var dropNextHandoff = false
    /// When false, a confirmed handoff request leaves the snapshot as it was (a lagging view).
    var applyHandoffs = true
    /// The activity the snapshot reports, in wire form.
    var activity: [String: Any] = ["working": false]
    /// Every request type received, in order.
    var received: [String] = []

    func connect() -> FakeTransport {
        let t = FakeTransport(runtime: self)
        lock.withLock { connections.append(t) }
        t.push(["type": "challenge", "proto": [1], "nonce": String(repeating: "aa", count: 32)])
        return t
    }

    func handle(_ msg: [String: Any], on t: FakeTransport) {
        let type = msg["type"] as? String ?? ""
        let id = msg["id"] as? Int ?? 0
        lock.lock()
        defer { lock.unlock() }
        if type != "hello" { received.append(type) }
        switch type {
        case "hello":
            t.push(["type": "welcome", "proto": 1, "session": "s", "surface": "mac",
                    "identity": ["agent_id": "stark", "uuid": uuid]])
        case "snapshot":
            t.push(["type": "result", "id": id, "data": [
                "server": "fake", "identity": ["agent_id": "stark", "uuid": uuid],
                "you": "mac",
                "presence": ["surface": surface, "home": "mac", "generation": generation],
                "handoff": handoff.map { $0 as Any } ?? NSNull(),
                "responsibilities": [], "conversation": ["id": Wire.conversation, "head_seq": rows.count - 1],
                "activity": activity]])
        case "sync":
            let after = msg["after_seq"] as? Int ?? -1
            t.push(["type": "result", "id": id, "data": [
                "conversation_id": Wire.conversation, "more": false,
                "rows": rows.filter { $0.seq > after }.map { ["seq": $0.seq, "role": $0.role, "content": $0.content] }]])
        case "send":
            let cmid = msg["client_msg_id"] as! String
            sends.append(cmid)
            if let seq = landed[cmid] {
                t.push(["type": "result", "id": id, "data": ["seq": seq, "replayed": true]])
                return
            }
            guard msg["presence_gen"] as? Int == generation, surface == "mac" else {
                t.push(["type": "error", "id": id, "code": "stale_presence", "message": "stale"])
                return
            }
            let seq = rows.count
            rows.append(Row(seq: seq, role: "user", content: msg["text"] as! String))
            landed[cmid] = seq
            if dropAfterNextSend {
                dropAfterNextSend = false
                t.hangUp()
                return
            }
            t.push(["type": "result", "id": id, "data": ["seq": seq, "replayed": false]])
        case "ready_out", "ack_in":
            handoffCalls.append(type)
            if dropNextHandoff {
                dropNextHandoff = false
                t.hangUp()
                return
            }
            guard var h = handoff else {
                t.push(["type": "error", "id": id, "code": "invalid", "message": "no handoff"])
                return
            }
            if applyHandoffs {
                if type == "ready_out" {
                    generation += 1
                    surface = h["to"] as! String
                    h["state"] = "committed"
                    h["to_gen"] = generation
                } else {
                    h["state"] = "acked"
                }
                handoff = h
            }
            t.push(["type": "result", "id": id, "data": [
                "handoff_id": h["id"]!, "state": h["state"]!, "to_gen": h["to_gen"] ?? NSNull()]])
        default:
            t.push(["type": "error", "id": id, "code": "unknown_command", "message": type])
        }
    }
}

final class FakeTransport: Transport, @unchecked Sendable {
    private let cond = NSCondition()
    private var inbox: [Data] = []
    private var closed = false
    private var reader = FrameReader()
    weak var runtime: FakeRuntime?

    init(runtime: FakeRuntime) { self.runtime = runtime }

    func push(_ obj: [String: Any]) {
        let frame = try! Frame.encode(json: obj)
        cond.withLock {
            inbox.append(frame)
            cond.signal()
        }
    }

    func hangUp() {
        cond.withLock {
            closed = true
            cond.broadcast()
        }
    }

    func send(_ frame: Data) throws {
        if cond.withLock({ closed }) { throw TransportFailure.closed }
        reader.feed(frame)
        while let body = try reader.next() {
            let obj = try JSONSerialization.jsonObject(with: body) as! [String: Any]
            runtime?.handle(obj, on: self)
        }
    }

    func receive() throws -> Data {
        cond.lock()
        defer { cond.unlock() }
        while inbox.isEmpty && !closed { cond.wait() }
        if !inbox.isEmpty { return inbox.removeFirst() }
        return Data()
    }

    func close() { hangUp() }
}

func tokenFile() throws -> URL {
    let url = FileManager.default.temporaryDirectory.appendingPathComponent("tok-\(UUID().uuidString)")
    try Data(String(repeating: "00", count: 32).utf8).write(to: url)
    try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    return url
}

func eventually(_ timeout: TimeInterval = 5, _ cond: () -> Bool) -> Bool {
    let end = Date().addingTimeInterval(timeout)
    while Date() < end {
        if cond() { return true }
        Thread.sleep(forTimeInterval: 0.01)
    }
    return cond()
}

@Suite(.serialized) struct ClientTests {
    func client(_ rt: FakeRuntime) throws -> GatewayClient {
        let cfg = ClientConfig(socketPath: "/fake", tokenURL: try tokenFile(),
                               backoff: Backoff(base: 0.01, cap: 0.05, jitter: 0))
        return GatewayClient(config: cfg, makeTransport: { _ in rt.connect() })
    }

    @Test func goesLiveWithIdentityPresenceAndRows() throws {
        let rt = FakeRuntime()
        let c = try client(rt)
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.connection == .live })
        let s = c.state
        #expect(s.identity?.uuid == "uuid-1")
        #expect(s.presence?.generation == 1 && s.isActiveSurface)
        #expect(s.rows.map(\.seq) == [0])
    }

    @Test func lostReplyIsResentAfterReconnectAndLandsOnce() throws {
        let rt = FakeRuntime()
        let c = try client(rt)
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.connection == .live })
        rt.lock.withLock { rt.dropAfterNextSend = true }
        let id = c.send("hello")
        #expect(eventually { c.state.outbox.first?.status == .sent(seq: 1) })
        rt.lock.withLock {
            #expect(rt.sends == [id, id])            // resent under the same id
            #expect(rt.rows.filter { $0.role == "user" }.count == 1)
            #expect(rt.connections.count == 2)
        }
        #expect(eventually { c.state.rows.map(\.seq) == [0, 1] })
    }

    @Test func reconnectResyncsWithoutDuplicates() throws {
        let rt = FakeRuntime()
        let c = try client(rt)
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.connection == .live })
        rt.lock.withLock {
            rt.rows.append(Row(seq: 1, role: "user", content: "a"))
            rt.rows.append(Row(seq: 2, role: "assistant", content: "b"))
        }
        c.reconnectNow()
        #expect(eventually { c.state.rows.map(\.seq) == [0, 1, 2] })
        c.reconnectNow()
        #expect(eventually { c.state.connection == .live && rt.connections.count == 3 })
        #expect(c.state.rows.map(\.seq) == [0, 1, 2])
    }

    @Test func obsoleteGenerationIsRejectedAndNothingLands() throws {
        let rt = FakeRuntime()
        let c = try client(rt)
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.connection == .live })
        c.send("from the past", presenceGen: 0)
        #expect(eventually { c.state.outbox.first?.status == .rejected(code: "stale_presence") })
        rt.lock.withLock { #expect(rt.rows.count == 1) }
    }

    @Test func newIdentityWipesTheMirror() throws {
        let rt = FakeRuntime()
        rt.rows.append(Row(seq: 1, role: "user", content: "old db"))
        let c = try client(rt)
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.rows.count == 2 })
        rt.lock.withLock {
            rt.uuid = "uuid-2"
            rt.rows = [Row(seq: 0, role: "system", content: "fresh")]
        }
        c.reconnectNow()
        #expect(eventually { c.state.identity?.uuid == "uuid-2" && c.state.rows.map(\.content) == ["fresh"] })
    }

    static var preparing: [String: Any] {
        ["id": "h1", "from": "mac", "to": "phone", "state": "preparing", "from_gen": 1]
    }

    @Test func unconfirmedReadyOutIsResentAfterReconnect() throws {
        let rt = FakeRuntime()
        rt.handoff = Self.preparing
        rt.dropNextHandoff = true
        let c = try client(rt)
        c.start()
        defer { c.stop() }
        #expect(eventually { rt.lock.withLock { rt.handoffCalls == ["ready_out", "ready_out"] } })
        #expect(eventually { rt.lock.withLock { rt.generation == 2 && rt.surface == "phone" } })
        rt.lock.withLock { #expect(rt.connections.count == 2) }
    }

    @Test func confirmedReadyOutIsNotResentAfterReconnect() throws {
        let rt = FakeRuntime()
        rt.handoff = Self.preparing
        rt.applyHandoffs = false                 // the snapshot keeps reporting preparing
        let c = try client(rt)
        c.start()
        defer { c.stop() }
        #expect(eventually { rt.lock.withLock { rt.handoffCalls == ["ready_out"] } })
        c.reconnectNow()
        #expect(eventually { c.state.connection == .live && rt.lock.withLock { rt.connections.count == 2 } })
        Thread.sleep(forTimeInterval: 0.2)
        rt.lock.withLock { #expect(rt.handoffCalls == ["ready_out"]) }
    }

    @Test func unconfirmedAckInIsResentAfterReconnect() throws {
        let rt = FakeRuntime()
        rt.generation = 2
        rt.handoff = ["id": "h1", "from": "phone", "to": "mac", "state": "committed",
                      "from_gen": 1, "to_gen": 2]
        rt.dropNextHandoff = true
        let c = try client(rt)
        c.start()
        defer { c.stop() }
        #expect(eventually { rt.lock.withLock { rt.handoffCalls == ["ack_in", "ack_in"] } })
        #expect(eventually { rt.lock.withLock { rt.handoff?["state"] as? String == "acked" } })
        c.reconnectNow()
        #expect(eventually { c.state.connection == .live && rt.lock.withLock { rt.connections.count == 3 } })
        Thread.sleep(forTimeInterval: 0.2)
        rt.lock.withLock { #expect(rt.handoffCalls == ["ack_in", "ack_in"]) }
    }

    func activity(_ state: String, boot: String, work: Int = 0, inference: Int = 0) -> [String: Any] {
        ["state": state, "boot": boot, "working": inference > 0, "owner_generation": 1,
         "inference": (0..<inference).map { ["call_id": "call:\($0)", "conversation_id": "c",
                                             "stage": "execute"] },
         "work": (0..<work).map { ["responsibility_id": "monitor:o/r:\($0)",
                                   "kind": "monitor_pull_request", "count": 1] },
         "blocked": [String]()]
    }

    func push(_ rt: FakeRuntime, _ data: [String: Any], rev: Int) {
        rt.lock.withLock { rt.connections.last }?.push(
            ["type": "event", "kind": "activity", "rev": rev, "data": data])
    }

    @Test func snapshotSeedsActivityAndPushesFollow() throws {
        let rt = FakeRuntime()
        rt.activity = activity("working", boot: "b1", work: 1, inference: 1)
        let c = try client(rt)
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.connection == .live && c.state.activity != nil })
        #expect(c.state.isWorking && c.state.isThinking)
        push(rt, activity("thinking", boot: "b1", inference: 1), rev: 1)
        #expect(eventually { !c.state.isWorking })
        #expect(c.state.isThinking && c.state.working)
        push(rt, activity("idle", boot: "b1"), rev: 2)
        #expect(eventually { !c.state.isThinking && !c.state.working })
    }

    @Test func pushFromAnotherRuntimeInstanceIsDropped() throws {
        let rt = FakeRuntime()
        rt.activity = activity("idle", boot: "b2")
        let c = try client(rt)
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.activity?.boot == "b2" })
        push(rt, activity("working", boot: "b1", work: 1), rev: 1)   // stale instance
        push(rt, activity("thinking", boot: "b2", inference: 1), rev: 2)
        #expect(eventually { c.state.isThinking })
        #expect(!c.state.isWorking && c.state.activity?.boot == "b2")
    }

    @Test func disconnectClearsActivityAndReconnectRebuildsFromSnapshot() throws {
        let rt = FakeRuntime()
        rt.activity = activity("working", boot: "b1", work: 2)
        let up = Locked(true)
        let cfg = ClientConfig(socketPath: "/fake", tokenURL: try tokenFile(),
                               backoff: Backoff(base: 0.01, cap: 0.05, jitter: 0))
        let c = GatewayClient(config: cfg, makeTransport: { _ in
            guard up.value else { throw TransportFailure.connect(ECONNREFUSED) }
            return rt.connect()
        })
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.isWorking })
        up.value = false                                 // runtime restarts
        rt.lock.withLock { rt.connections.last }?.hangUp()
        #expect(eventually { !c.state.connection.isLive })
        #expect(c.state.activity == nil && !c.state.isWorking && !c.state.working)
        rt.lock.withLock { rt.activity = activity("thinking", boot: "b2", inference: 1) }
        up.value = true
        #expect(eventually { c.state.connection == .live && c.state.activity?.boot == "b2" })
        #expect(c.state.isThinking && !c.state.isWorking)
    }

    @Test func activityPushesSendNothingToTheRuntime() throws {
        let rt = FakeRuntime()
        rt.activity = activity("idle", boot: "b1")
        let c = try client(rt)
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.connection == .live })
        #expect(eventually { rt.lock.withLock { rt.received.count } >= 2 })   // snapshot, sync
        Thread.sleep(forTimeInterval: 0.1)
        let before = rt.lock.withLock { rt.received }
        for i in 1...50 {
            push(rt, activity(i % 2 == 0 ? "working" : "idle", boot: "b1", work: 1 - i % 2), rev: i)
        }
        #expect(eventually { c.state.activity?.state == "working" })
        Thread.sleep(forTimeInterval: 0.1)
        #expect(rt.lock.withLock { rt.received } == before)
    }

    @Test func runtimeDownKeepsRetryingThenRecovers() throws {
        let rt = FakeRuntime()
        let up = Locked(false)
        let cfg = ClientConfig(socketPath: "/fake", tokenURL: try tokenFile(),
                               backoff: Backoff(base: 0.01, cap: 0.05, jitter: 0))
        let c = GatewayClient(config: cfg, makeTransport: { _ in
            guard up.value else { throw TransportFailure.connect(ECONNREFUSED) }
            return rt.connect()
        })
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.connection == .disconnected("runtime unreachable") })
        up.value = true
        #expect(eventually { c.state.connection == .live })
    }
}

final class Locked<T: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var v: T
    init(_ v: T) { self.v = v }
    var value: T {
        get { lock.withLock { v } }
        set { lock.withLock { v = newValue } }
    }
}
