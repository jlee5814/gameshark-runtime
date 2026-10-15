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
        switch type {
        case "hello":
            t.push(["type": "welcome", "proto": 1, "session": "s", "surface": "mac",
                    "identity": ["agent_id": "stark", "uuid": uuid]])
        case "snapshot":
            t.push(["type": "result", "id": id, "data": [
                "server": "fake", "identity": ["agent_id": "stark", "uuid": uuid],
                "you": "mac",
                "presence": ["surface": surface, "home": "mac", "generation": generation],
                "responsibilities": [], "conversation": ["id": Wire.conversation, "head_seq": rows.count - 1],
                "activity": ["working": false]]])
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
