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
    /// The pinned key the challenge names; a paired device checks it before its hello.
    var serverFP: String?
    /// The surface a device hello is bound to (the runtime's device row).
    var deviceSurface = "pocket-a1b2c3"
    /// When set, requests vanish unanswered: a socket whose network is gone.
    var blackhole = false
    /// Paired device names the snapshot reports.
    var devices: [[String: Any]] = []
    /// The draft awaiting this surface's receipt, as the snapshot and ack_in report it.
    var draft: [String: Any]?
    /// When false, a confirmed draft stays in the snapshot (a lagging view).
    var applyDraftAcks = true
    /// When set, the next draft_ack is lost on the way and the connection drops.
    var dropNextDraftAck = false
    /// When set, draft_ack is refused as stale.
    var staleDraftAcks = false
    /// Every draft_ack's handoff id, and every ready_out's draft, in order.
    var draftAcks: [String] = []
    var readyOutDrafts: [String?] = []
    /// The draft the open jack committed with, as the handoff row holds it.
    var committedDraft: String?
    /// When set with dropNextHandoff, the prepare deadline commits the jack without a
    /// draft while the lost ready_out is on its way.
    var expireOnDrop = false
    /// When set, the prepare deadline commits the jack without a draft before the next
    /// ready_out arrives.
    var deadlineFirst = false
    /// When set, the next ready_out commits but its reply is lost and the connection drops.
    var dropReadyOutReply = false
    /// When set, ready_out is refused as stale.
    var staleReadyOut = false
    /// The last jack in wire form, kept after it closes so a replay sees its final state.
    var lastJack: [String: Any]?

    /// The open jack resolves (acked by its target, or reverted) and presence moves to
    /// holder; the push tells every connection the jack closed.
    func closeJack(_ state: String, holder: String) {
        guard var h = handoff ?? lastJack else { return }
        h["state"] = state
        lastJack = h
        handoff = nil
        if state == "reverted" { generation += 1 }
        surface = holder
        for t in connections {
            t.push(["type": "event", "kind": "presence", "rev": 9, "data": [
                "presence": ["surface": surface, "home": "mac", "generation": generation],
                "handoff": NSNull()]])
        }
    }

    /// The trusted deadline path: commit the open preparing jack without a draft.
    func expirePrepare() {
        guard var h = handoff, h["state"] as? String == "preparing" else { return }
        generation += 1
        surface = h["to"] as! String
        h["state"] = "committed"
        h["to_gen"] = generation
        handoff = h
        committedDraft = nil
    }

    func connect() -> FakeTransport {
        let t = FakeTransport(runtime: self)
        lock.withLock { connections.append(t) }
        var challenge: [String: Any] = ["type": "challenge", "proto": [1],
                                         "nonce": String(repeating: "aa", count: 32)]
        if let fp = lock.withLock({ serverFP }) { challenge["server_fp"] = fp }
        t.push(challenge)
        return t
    }

    func handle(_ msg: [String: Any], on t: FakeTransport) {
        let type = msg["type"] as? String ?? ""
        let id = msg["id"] as? Int ?? 0
        lock.lock()
        defer { lock.unlock() }
        if blackhole && type != "hello" { return }
        if type != "hello" { received.append(type) }
        switch type {
        case "hello":
            let bound = msg["device_id"] != nil ? deviceSurface : "mac"
            t.push(["type": "welcome", "proto": 1, "session": "s",
                    "surface": msg["surface"] as? String ?? bound,
                    "identity": ["agent_id": "stark", "uuid": uuid]])
        case "snapshot":
            t.push(["type": "result", "id": id, "data": [
                "server": "fake", "identity": ["agent_id": "stark", "uuid": uuid],
                "you": "mac",
                "presence": ["surface": surface, "home": "mac", "generation": generation],
                "handoff": handoff.map { $0 as Any } ?? NSNull(),
                "responsibilities": [], "conversation": ["id": Wire.conversation, "head_seq": rows.count - 1],
                "activity": activity, "devices": devices,
                "draft": draft.map { $0 as Any } ?? NSNull()]])
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
        case "draft_ack":
            draftAcks.append(msg["handoff_id"] as? String ?? "")
            if dropNextDraftAck {
                dropNextDraftAck = false
                t.hangUp()
                return
            }
            if staleDraftAcks {
                t.push(["type": "error", "id": id, "code": "stale_presence", "message": "stale"])
                return
            }
            if applyDraftAcks { draft = nil }
            t.push(["type": "result", "id": id, "data": ["handoff_id": msg["handoff_id"]!,
                                                         "draft_state": "delivered"]])
        case "ready_out", "ack_in":
            handoffCalls.append(type)
            if type == "ready_out" { readyOutDrafts.append(msg["draft"] as? String) }
            if dropNextHandoff {
                dropNextHandoff = false
                if expireOnDrop { expirePrepare() }
                t.hangUp()
                return
            }
            if type == "ready_out" && deadlineFirst {
                deadlineFirst = false
                expirePrepare()
            }
            if type == "ready_out" && staleReadyOut {
                t.push(["type": "error", "id": id, "code": "stale_presence", "message": "stale"])
                return
            }
            let open = handoff
            guard var h = open ?? lastJack.flatMap({ $0["id"] as? String == msg["handoff_id"] as? String ? $0 : nil })
            else {
                t.push(["type": "error", "id": id, "code": "invalid", "message": "no handoff"])
                return
            }
            let sentText = msg["draft"] as? String
            if applyHandoffs {
                if type == "ready_out" {
                    if h["state"] as? String == "preparing" {
                        generation += 1
                        surface = h["to"] as! String
                        h["state"] = "committed"
                        h["to_gen"] = generation
                        committedDraft = sentText.flatMap { $0.isEmpty ? nil : $0 }
                    }
                } else {
                    h["state"] = "acked"
                }
                if open != nil { handoff = h }
                lastJack = h
            }
            if type == "ready_out" && dropReadyOutReply {
                dropReadyOutReply = false
                t.hangUp()
                return
            }
            let landed = ["committed", "acked"].contains(h["state"] as? String ?? "")
            let carried = landed && committedDraft != nil && committedDraft == sentText
            t.push(["type": "result", "id": id, "data": [
                "handoff_id": h["id"]!, "state": h["state"]!, "to_gen": h["to_gen"] ?? NSNull(),
                "draft": type == "ack_in" ? (draft?["text"] ?? NSNull()) : NSNull(),
                "draft_carried": carried]])
        case "jack":
            // A silent source: the runtime commits at once and the caller must ack.
            guard msg["to"] as? String == "self", msg["presence_gen"] as? Int == generation,
                  surface != "mac"
            else {
                t.push(["type": "error", "id": id, "code": "stale_presence", "message": "stale"])
                return
            }
            let from = surface
            generation += 1
            surface = "mac"
            let h: [String: Any] = ["id": msg["handoff_id"]!, "from": from, "to": "mac",
                                    "state": "committed", "from_gen": generation - 1,
                                    "to_gen": generation]
            handoff = h
            t.push(["type": "result", "id": id, "data": ["handoff_id": h["id"]!,
                                                         "state": "preparing"]])
            t.push(["type": "event", "kind": "presence", "rev": 1, "data": [
                "presence": ["surface": surface, "home": "mac", "generation": generation],
                "handoff": h]])
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

    @Test func jackHereOpensTheHandoffThenAcksAsTarget() throws {
        let rt = FakeRuntime()
        rt.surface = "pocket-a1b2c3"                  // Stark is on the phone
        rt.devices = [["surface": "pocket-a1b2c3", "name": "Jae's iPhone"]]
        let c = try client(rt)
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.connection == .live })
        #expect(!c.state.isActiveSurface)
        #expect(c.state.holderName == "Jae's iPhone")
        c.jack(.here)
        #expect(eventually { c.state.isActiveSurface })
        #expect(eventually { rt.lock.withLock { rt.received.contains("ack_in") } })
        let order = rt.lock.withLock { rt.received.filter { ["jack", "ack_in"].contains($0) } }
        #expect(order == ["jack", "ack_in"])
        #expect(c.state.holderName == nil)
    }

    @Test func jackWhileDisconnectedSendsNothing() throws {
        let rt = FakeRuntime()
        let c = try client(rt)
        c.jack(.here)                                 // not started
        Thread.sleep(forTimeInterval: 0.05)
        #expect(c.state.lastError == "jack: not connected")
        #expect(rt.lock.withLock { rt.received.isEmpty })
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

// MARK: draft continuity

extension ClientTests {
    static var committedToMac: [String: Any] {
        ["id": "h1", "from": "phone", "to": "mac", "state": "committed", "from_gen": 1,
         "to_gen": 2]
    }

    func arriving(_ rt: FakeRuntime) {
        rt.surface = "mac"
        rt.generation = 2
        rt.handoff = Self.committedToMac
        rt.draft = ["handoff_id": "h1", "text": "from my pocket"]
    }

    @Test func aCarriedDraftArrivesWithTheAckAndIsConfirmedOnce() throws {
        let rt = FakeRuntime()
        arriving(rt)
        let c = try client(rt)
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.incomingDraft == CarriedDraft(handoffId: "h1", text: "from my pocket") })
        c.confirmDraft("h1")
        #expect(eventually { rt.lock.withLock { rt.draftAcks == ["h1"] } })
        #expect(c.state.incomingDraft == nil)
        c.reconnectNow()
        #expect(eventually { c.state.connection == .live && rt.lock.withLock { rt.connections.count == 2 } })
        Thread.sleep(forTimeInterval: 0.1)
        #expect(c.state.incomingDraft == nil)
        rt.lock.withLock { #expect(rt.draftAcks == ["h1"]) }
    }

    @Test func aLostReceiptIsResentAfterReconnect() throws {
        let rt = FakeRuntime()
        arriving(rt)
        rt.dropNextDraftAck = true
        let c = try client(rt)
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.incomingDraft != nil })
        c.confirmDraft("h1")
        #expect(eventually { rt.lock.withLock { rt.draftAcks == ["h1", "h1"] && rt.draft == nil } })
        #expect(c.state.incomingDraft == nil)                   // never offered twice
    }

    @Test func anUnconfirmedDraftIsOfferedAgainAfterReconnect() throws {
        let rt = FakeRuntime()
        rt.draft = ["handoff_id": "h0", "text": "kept by the runtime"]
        let c = try client(rt)
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.incomingDraft?.text == "kept by the runtime" })
        c.reconnectNow()
        #expect(eventually { c.state.connection == .live && rt.lock.withLock { rt.connections.count == 2 } })
        #expect(c.state.incomingDraft?.handoffId == "h0")
    }

    @Test func aConfirmedDraftStaysConfirmedWhileTheSnapshotLags() throws {
        let rt = FakeRuntime()
        rt.draft = ["handoff_id": "h0", "text": "t"]
        rt.applyDraftAcks = false
        let c = try client(rt)
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.incomingDraft != nil })
        c.confirmDraft("h0")
        #expect(eventually { rt.lock.withLock { rt.draftAcks == ["h0"] } })
        c.reconnectNow()
        #expect(eventually { c.state.connection == .live && rt.lock.withLock { rt.connections.count == 2 } })
        Thread.sleep(forTimeInterval: 0.1)
        #expect(c.state.incomingDraft == nil)
    }

    @Test func aStaleReceiptIsFinalAndTheOfferIsWithdrawn() throws {
        let rt = FakeRuntime()
        arriving(rt)
        rt.staleDraftAcks = true
        let c = try client(rt)
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.incomingDraft != nil })
        c.confirmDraft("h1")
        #expect(eventually { rt.lock.withLock { rt.draftAcks == ["h1"] } })
        c.reconnectNow()
        #expect(eventually { c.state.connection == .live && rt.lock.withLock { rt.connections.count == 2 } })
        Thread.sleep(forTimeInterval: 0.1)
        rt.lock.withLock { #expect(rt.draftAcks == ["h1"]) }  // not retried
    }

    @Test func theOfferIsWithdrawnWhenPresenceLeaves() throws {
        let rt = FakeRuntime()
        arriving(rt)
        let c = try client(rt)
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.incomingDraft != nil })
        rt.lock.withLock {
            rt.surface = "phone"                       // the runtime no longer offers it here
            rt.generation = 3
            rt.draft = nil
            rt.connections.last?.push(["type": "event", "kind": "presence", "rev": 2, "data": [
                "presence": ["surface": "phone", "home": "mac", "generation": 3],
                "handoff": NSNull()]])
        }
        #expect(eventually { c.state.incomingDraft == nil })
    }

    @Test func readyOutRecordsTheDraftThatLeft() throws {
        let rt = FakeRuntime()
        rt.handoff = Self.preparing
        let c = try client(rt)
        c.setDraftProvider { "half typed" }
        c.start()
        defer { c.stop() }
        #expect(eventually { rt.lock.withLock { rt.readyOutDrafts == ["half typed"] } })
        #expect(eventually { rt.lock.withLock { rt.handoff?["state"] as? String == "committed" } })
        Thread.sleep(forTimeInterval: 0.1)
        #expect(c.state.sentDraft == nil)        // committed is not yet arrived
        rt.lock.withLock { rt.closeJack("acked", holder: "phone") }
        #expect(eventually { c.state.sentDraft == CarriedDraft(handoffId: "h1", text: "half typed") })
        c.clearSentDraft("h1")
        #expect(eventually { c.state.sentDraft == nil })
    }

    // A source may clear its composer only on the runtime's receipt for that exact text.

    @Test func readyOutLostThenDeadlineCommitKeepsComposer() throws {
        let rt = FakeRuntime()
        rt.handoff = Self.preparing
        rt.dropNextHandoff = true
        rt.expireOnDrop = true                  // the deadline commits while it is lost
        let c = try client(rt)
        c.setDraftProvider { "only copy" }
        c.start()
        defer { c.stop() }
        #expect(eventually { rt.lock.withLock { rt.connections.count == 2 } && c.state.connection == .live })
        #expect(eventually { rt.lock.withLock { rt.handoffCalls.count == 2 } })   // reconciled
        Thread.sleep(forTimeInterval: 0.1)
        #expect(c.state.sentDraft == nil)
        var s = c.state
        s.handoff = nil                          // the jack closes at the phone
        let steps = DraftContinuity.follow(s, composer: "only copy", offered: nil)
        #expect(steps.composer == nil && steps.clearSent == nil)
    }

    @Test func readyOutRefusedKeepsComposer() throws {
        let rt = FakeRuntime()
        rt.handoff = Self.preparing
        rt.staleReadyOut = true
        let c = try client(rt)
        c.setDraftProvider { "only copy" }
        c.start()
        defer { c.stop() }
        #expect(eventually { rt.lock.withLock { rt.handoffCalls == ["ready_out"] } })
        Thread.sleep(forTimeInterval: 0.1)
        #expect(c.state.sentDraft == nil)
    }

    @Test func readyOutThatLosesToTheDeadlineKeepsComposer() throws {
        let rt = FakeRuntime()
        rt.handoff = Self.preparing
        rt.deadlineFirst = true
        let c = try client(rt)
        c.setDraftProvider { "only copy" }
        c.start()
        defer { c.stop() }
        #expect(eventually { rt.lock.withLock { rt.handoffCalls == ["ready_out"] } })
        Thread.sleep(forTimeInterval: 0.1)
        #expect(c.state.sentDraft == nil)
    }

    @Test func lostReadyOutReplyReconcilesOnReconnect() throws {
        let rt = FakeRuntime()
        rt.handoff = Self.preparing
        rt.dropReadyOutReply = true
        let text = Locked("half typed")
        let c = try client(rt)
        c.setDraftProvider { text.value }
        c.start()
        defer { c.stop() }
        #expect(eventually { rt.lock.withLock { rt.connections.count == 2 } })
        text.value = "typed after the jack"     // newer edits never replace the receipt
        rt.lock.withLock { rt.closeJack("acked", holder: "phone") }
        #expect(eventually { c.state.sentDraft == CarriedDraft(handoffId: "h1", text: "half typed") })
        rt.lock.withLock { #expect(rt.readyOutDrafts.allSatisfy { $0 == "half typed" }) }
    }

    @Test func aJackThatRevertsElsewhereKeepsTheSourceComposer() throws {
        // The phone carries its draft to a pad; the pad never acks and Stark reverts home,
        // away from the phone. The text arrived nowhere, so the phone keeps it.
        let rt = FakeRuntime()
        rt.surface = "phone"
        rt.handoff = ["id": "h1", "from": "phone", "to": "pad", "state": "preparing",
                      "from_gen": 1]
        let cfg = ClientConfig(socketPath: "/fake", tokenURL: try tokenFile(), surface: "phone",
                               backoff: Backoff(base: 0.01, cap: 0.05, jitter: 0))
        let c = GatewayClient(config: cfg, makeTransport: { _ in rt.connect() })
        c.setDraftProvider { "only copy" }
        c.start()
        defer { c.stop() }
        #expect(eventually { rt.lock.withLock { rt.handoff?["state"] as? String == "committed" } })
        Thread.sleep(forTimeInterval: 0.1)
        rt.lock.withLock { rt.closeJack("reverted", holder: "mac") }
        #expect(eventually { c.state.presence?.surface == "mac" && c.state.handoff == nil })
        Thread.sleep(forTimeInterval: 0.2)
        #expect(c.state.sentDraft == nil)
        let steps = DraftContinuity.follow(c.state, composer: "only copy", offered: nil)
        #expect(steps.composer == nil && steps.clearSent == nil)
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

// MARK: network paths (Wi-Fi, cellular, Tailscale)

extension ClientTests {
    static let pin = Data(repeating: 0xAB, count: 32)

    func device(_ rt: FakeRuntime, routes: [Route], uuid: String = "uuid-1",
                dialed: Locked<[Route]>, reachable: @escaping @Sendable (Route) -> Bool)
        throws -> GatewayClient
    {
        rt.serverFP = Self.pin.hex
        let cred = DeviceCredential(deviceID: String(repeating: "d", count: 32),
                                    surface: "pocket-a1b2c3", name: "Test",
                                    hosts: routes.filter { $0.via == .lan }.map(\.host),
                                    port: 47820, pin: Self.pin, agentUUID: uuid, routes: routes)
        let signer = try SoftwareSigner(rawRepresentation: Data(1...32))
        let cfg = ClientConfig(device: DeviceEndpoint(credential: cred, signer: signer),
                               backoff: Backoff(base: 0.01, cap: 0.05, jitter: 0),
                               clientName: "test")
        return GatewayClient(config: cfg, makeTransport: { r in
            dialed.value.append(r)
            guard reachable(r) else { throw TransportFailure.connect(ETIMEDOUT) }
            return rt.connect()
        })
    }

    static let home = [Route(host: "192.168.1.20", port: 47820, via: .lan),
                       Route(host: "Jaes-Mac.local", port: 47820, via: .lan),
                       Route(host: "100.123.57.23", port: 47821, via: .tailnet),
                       Route(host: "mac.tail7fe60e.ts.net", port: 47821, via: .tailnet)]

    @Test func aDeadLanAddressFallsBackToTheTailnetRouteOnItsOwnPort() throws {
        let rt = FakeRuntime()
        let dialed = Locked<[Route]>([])
        let c = try device(rt, routes: Self.home, dialed: dialed) { $0.via == .tailnet }
        c.setPath(.wifi)
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.connection == .live })
        #expect(Array(dialed.value.prefix(3)) == Array(Self.home.prefix(3)))
        #expect(c.state.surface == "pocket-a1b2c3")
    }

    @Test func aReconnectRedialsTheRouteThatLastWorked() throws {
        let rt = FakeRuntime()
        let dialed = Locked<[Route]>([])
        let c = try device(rt, routes: Self.home, dialed: dialed) { $0.via == .tailnet }
        c.setPath(.wifi)
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.connection == .live })
        let before = dialed.value.count
        rt.lock.withLock { rt.connections.last }?.hangUp()      // runtime restarts
        #expect(eventually { rt.lock.withLock { rt.connections.count } == 2 })
        #expect(eventually { c.state.connection == .live })
        #expect(dialed.value[before] == Self.home[2])           // no detour through the LAN
        c.setPath(.cellular)                                     // a new network starts over
        #expect(eventually { rt.lock.withLock { rt.connections.count } == 3 })
        #expect(dialed.value.last == Self.home[2])
    }

    @Test func onCellularTheTailnetIsDialedFirst() throws {
        let rt = FakeRuntime()
        let dialed = Locked<[Route]>([])
        let c = try device(rt, routes: Self.home, dialed: dialed) { _ in true }
        c.setPath(.cellular)
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.connection == .live })
        #expect(dialed.value == [Self.home[2]])
    }

    @Test func routeOrderFollowsTheNetwork() {
        let lan = Self.home.filter { $0.via == .lan }, tail = Self.home.filter { $0.via == .tailnet }
        #expect(Route.ordered(Self.home, for: .wifi) == lan + tail)
        #expect(Route.ordered(Self.home, for: .wired) == lan + tail)
        #expect(Route.ordered(Self.home, for: .cellular) == tail + lan)
        #expect(Route.ordered(Self.home, for: .other) == tail + lan)
        #expect(Route.ordered(Self.home, for: .none) == Self.home)
    }

    @Test func movingToCellularReconnectsOnceAndResendsUnderTheSameId() throws {
        let rt = FakeRuntime()
        let c = try client(rt)
        c.setPath(.wifi)
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.connection == .live })
        rt.lock.withLock { rt.blackhole = true }           // Wi-Fi gone, socket still open
        let id = c.send("from the platform")
        Thread.sleep(forTimeInterval: 0.1)
        #expect(c.state.outbox.first?.status == .pending)
        rt.lock.withLock { rt.blackhole = false }
        c.setPath(.cellular)
        #expect(eventually { c.state.outbox.first?.status == .sent(seq: 1) })
        rt.lock.withLock {
            #expect(rt.connections.count == 2)
            #expect(rt.sends == [id])
            #expect(rt.rows.filter { $0.role == "user" }.count == 1)
        }
    }

    @Test func aPathUpdateOnTheSameNetworkKeepsTheSession() throws {
        let rt = FakeRuntime()
        let c = try client(rt)
        c.setPath(.wifi)
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.connection == .live })
        c.setPath(.wifi)
        c.setPath(.none)                                  // offline: nothing to dial yet
        Thread.sleep(forTimeInterval: 0.2)
        #expect(rt.lock.withLock { rt.connections.count } == 1)
        c.setPath(.wifi)                                  // back on the same network
        #expect(eventually { rt.lock.withLock { rt.connections.count } == 2 })
        #expect(eventually { c.state.connection == .live })
    }

    @Test func aDeviceRefusesAnotherAgentsRuntime() throws {
        let rt = FakeRuntime()
        rt.uuid = "uuid-other"
        rt.rows.append(Row(seq: 1, role: "user", content: "not ours"))
        let dialed = Locked<[Route]>([])
        let c = try device(rt, routes: Self.home, dialed: dialed) { _ in true }
        c.start()
        defer { c.stop() }
        #expect(eventually { c.state.lastError?.contains("another agent") == true })
        Thread.sleep(forTimeInterval: 0.1)
        #expect(c.state.connection != .live && c.state.rows.isEmpty)
        #expect(c.state.identity == nil)
        #expect(rt.lock.withLock { rt.received.isEmpty })  // no snapshot, no sync
    }
}
