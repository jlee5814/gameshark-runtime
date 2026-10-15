import Foundation

/// Exponential reconnect delay with jitter, reset once a session goes live.
public struct Backoff: Sendable {
    public var base: TimeInterval = 0.25
    public var cap: TimeInterval = 5.0
    public var jitter: Double = 0.2
    public private(set) var attempt = 0

    public init(base: TimeInterval = 0.25, cap: TimeInterval = 5.0, jitter: Double = 0.2) {
        self.base = base
        self.cap = cap
        self.jitter = jitter
    }

    /// The next delay; unit is a uniform sample in 0...1 (injected for tests).
    public mutating func next(unit: Double = Double.random(in: 0...1)) -> TimeInterval {
        let raw = min(cap, base * pow(2, Double(attempt)))
        attempt += 1
        return raw * (1 - jitter + 2 * jitter * unit)
    }

    public mutating func reset() { attempt = 0 }
}

public enum Connection: Equatable, Sendable {
    case disconnected(String?)
    case connecting
    case authenticating
    case syncing
    case live

    public var isLive: Bool { self == .live }
}

public struct OutgoingMessage: Equatable, Sendable, Identifiable {
    public enum Status: Equatable, Sendable {
        case pending
        case sent(seq: Int)
        case rejected(code: String)
    }

    public var id: String           // client_msg_id; resends reuse it, so the runtime dedups
    public var text: String
    public var status: Status
    var presenceGenOverride: Int?
}

/// Everything the UI renders, rebuilt from the runtime after every change. It holds no
/// agent state of its own: identity, presence, duties, and rows all come from the runtime.
public struct ClientState: Equatable, Sendable {
    public var surface: String
    public var connection: Connection = .disconnected(nil)
    public var identity: Identity?
    public var presence: PresenceInfo?
    public var handoff: HandoffInfo?
    public var responsibilities: [Responsibility] = []
    public var rows: [Row] = []
    public var working = false
    /// The runtime's activity projection; nil until this session's snapshot lands.
    public var activity: Activity?
    public var outbox: [OutgoingMessage] = []
    public var lastError: String?

    public var isActiveSurface: Bool { presence?.surface == surface }
    public var isWorking: Bool { activity?.isWorking ?? false }
    public var isThinking: Bool { activity?.isThinking ?? working }

    public init(surface: String) {
        self.surface = surface
    }
}

public struct ClientConfig: Sendable {
    public var socketPath: String
    public var tokenURL: URL
    public var surface: String
    public var backoff: Backoff

    public init(socketPath: String, tokenURL: URL, surface: String = "mac",
                backoff: Backoff = Backoff()) {
        self.socketPath = socketPath
        self.tokenURL = tokenURL
        self.surface = surface
        self.backoff = backoff
    }

    /// Same rule as stark.py: socket from STARK_SOCKET, else ~/.gameshark/gateway/stark.sock
    /// (the private directory beside the default database); token from STARK_TOKEN, else
    /// gateway.token beside the socket.
    public static func fromEnvironment(_ env: [String: String] = ProcessInfo.processInfo.environment)
        -> ClientConfig
    {
        let home = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".gameshark")
        let sock = env["STARK_SOCKET"] ?? home.appendingPathComponent("gateway/stark.sock").path
        let token = env["STARK_TOKEN"].map { URL(fileURLWithPath: $0) }
            ?? URL(fileURLWithPath: sock).deletingLastPathComponent()
            .appendingPathComponent("gateway.token")
        return ClientConfig(socketPath: sock, tokenURL: token)
    }
}

/// Reconnecting gateway client. All state lives on one serial queue; a reader thread per
/// connection feeds bytes into it. On every live session it resyncs rows after the last
/// contiguous seq it holds and resends unacknowledged messages under their original ids.
public final class GatewayClient: @unchecked Sendable {
    public typealias MakeTransport = @Sendable (String) throws -> Transport

    private enum Pending {
        case snapshot, sync, send(String), readyOut(String), ackIn(String)
    }

    private let config: ClientConfig
    private let makeTransport: MakeTransport
    private let onChange: @Sendable (ClientState) -> Void
    private let queue = DispatchQueue(label: "stark.gateway")

    // Queue confined.
    private var st: ClientState
    private var mirror = ConversationMirror()
    private var backoff: Backoff
    private var transport: Transport?
    private var reader = FrameReader()
    private var connId = 0
    private var nextId = 1
    private var pending: [Int: Pending] = [:]
    private var inFlight: Set<String> = []
    private var haveSnapshot = false
    private var haveSync = false
    /// Handoff phases (id + state) requested on this connection and awaiting a result.
    /// Cleared on reconnect, so a request lost with its connection is sent again.
    private var handoffSent: Set<String> = []
    /// Handoff phases the runtime answered. Kept across reconnects: the runtime already
    /// applied them, so they are never resent.
    private var handoffDone: Set<String> = []
    private var running = false
    private var draft: @Sendable () -> String? = { nil }

    public init(config: ClientConfig,
                makeTransport: @escaping MakeTransport = { try SocketTransport(path: $0) },
                onChange: @escaping @Sendable (ClientState) -> Void = { _ in }) {
        self.config = config
        self.makeTransport = makeTransport
        self.onChange = onChange
        self.backoff = config.backoff
        self.st = ClientState(surface: config.surface)
    }

    public var state: ClientState { queue.sync { st } }

    public func start() {
        queue.async {
            guard !self.running else { return }
            self.running = true
            self.connect()
        }
    }

    public func stop() {
        queue.sync {
            self.running = false
            self.drop(reason: "stopped", reconnect: false)
        }
    }

    /// Drop the current session and reconnect at once (the menu's Reconnect, and tests).
    public func reconnectNow() {
        queue.async {
            self.drop(reason: "reconnecting", reconnect: false)
            self.backoff.reset()
            if self.running { self.connect() }
        }
    }

    /// Supplies the unsent draft when this surface is asked to jack out.
    public func setDraftProvider(_ provider: @escaping @Sendable () -> String?) {
        queue.async { self.draft = provider }
    }

    /// Queue a user message. presenceGen overrides the held generation, which exists only to
    /// demonstrate that the runtime refuses an obsolete one.
    @discardableResult
    public func send(_ text: String, presenceGen: Int? = nil) -> String {
        let id = "mac-" + UUID().uuidString.lowercased()
        queue.async {
            self.st.outbox.append(OutgoingMessage(id: id, text: text, status: .pending,
                                                  presenceGenOverride: presenceGen))
            self.flushOutbox()
            self.publish()
        }
        return id
    }

    // MARK: connection

    private func connect() {
        connId += 1
        let id = connId
        st.connection = .connecting
        publish()
        let t: Transport
        do {
            t = try makeTransport(config.socketPath)
        } catch {
            st.connection = .disconnected("runtime unreachable")
            publish()
            scheduleReconnect(id)
            return
        }
        transport = t
        reader = FrameReader()
        pending = [:]
        inFlight = []
        handoffSent = []
        haveSnapshot = false
        haveSync = false
        clearActivity()
        st.connection = .authenticating
        publish()
        let thread = Thread { [weak self] in
            while true {
                let bytes: Data
                do { bytes = try t.receive() } catch { bytes = Data() }
                guard let self else { return }
                let closed = bytes.isEmpty
                self.queue.async { self.ingest(bytes, id) }
                if closed { return }
            }
        }
        thread.name = "stark.gateway.reader"
        thread.start()
    }

    private func drop(reason: String, reconnect: Bool) {
        transport?.close()
        transport = nil
        connId += 1
        clearActivity()
        if st.connection != .disconnected(reason) {
            st.connection = .disconnected(reason)
            publish()
        }
        if reconnect && running { scheduleReconnect(connId) }
    }

    private func scheduleReconnect(_ id: Int) {
        guard running else { return }
        let delay = backoff.next()
        queue.asyncAfter(deadline: .now() + delay) {
            guard self.running, self.connId == id, self.transport == nil else { return }
            self.connect()
        }
    }

    private func ingest(_ bytes: Data, _ id: Int) {
        guard id == connId else { return }          // a reader from an older connection
        if bytes.isEmpty {
            drop(reason: "runtime closed the connection", reconnect: true)
            return
        }
        reader.feed(bytes)
        do {
            while let body = try reader.next() {
                try process(body)
                guard id == connId else { return }
            }
        } catch {
            st.lastError = "protocol: \(error)"
            drop(reason: "protocol error", reconnect: true)
        }
    }

    private func write(_ object: [String: Any]) {
        guard let t = transport else { return }
        do {
            try t.send(Frame.encode(json: object))
        } catch {
            drop(reason: "write failed", reconnect: true)
        }
    }

    private func request(_ type: String, _ pendingKind: Pending, _ fields: [String: Any] = [:]) {
        let id = nextId
        nextId += 1
        pending[id] = pendingKind
        var msg = fields
        msg["type"] = type
        msg["id"] = id
        write(msg)
    }

    // MARK: frames

    private func process(_ body: Data) throws {
        let head = try Wire.decode(Head.self, from: body)
        switch head.type {
        case "challenge":
            let c = try Wire.decode(Challenge.self, from: body)
            guard c.proto.contains(Wire.protocolVersion), let nonce = Data(hex: c.nonce) else {
                st.lastError = "runtime speaks protocol \(c.proto), StarkMac speaks \(Wire.protocolVersion)"
                drop(reason: "unsupported version", reconnect: true)
                return
            }
            let token: Data
            do { token = try Auth.loadToken(at: config.tokenURL) } catch {
                st.lastError = "token: \(error)"
                drop(reason: "no token", reconnect: true)
                return
            }
            write(["type": "hello", "proto": Wire.protocolVersion, "surface": config.surface,
                   "client": "StarkMac/0.4",
                   "mac": Auth.sign(token: token, nonce: nonce, surface: config.surface,
                                    proto: Wire.protocolVersion)])
        case "welcome":
            let w = try Wire.decode(Welcome.self, from: body)
            if let known = st.identity, known.uuid != w.identity.uuid {
                // Another runtime database: nothing cached belongs to it.
                mirror.reset()
                st.rows = []
                st.outbox.removeAll { if case .pending = $0.status { false } else { true } }
            }
            st.identity = w.identity
            st.connection = .syncing
            st.lastError = nil
            request("snapshot", .snapshot)
            requestSync()
            publish()
        case "result":
            guard let id = head.id, let kind = pending.removeValue(forKey: id) else { return }
            try result(kind, body)
        case "error":
            let e = try Wire.decode(ErrorFrame.self, from: body)
            if let id = head.id, let kind = pending.removeValue(forKey: id) {
                failed(kind, code: e.code, message: e.message)
            } else {
                st.lastError = "\(e.code): \(e.message ?? "")"
                drop(reason: e.code, reconnect: true)
            }
        case "event":
            try event(head.kind ?? "", body)
        case "bye":
            drop(reason: "runtime stopping", reconnect: true)
        default:
            break
        }
    }

    private func requestSync() {
        request("sync", .sync, ["conversation_id": Wire.conversation,
                                "after_seq": mirror.contiguousHead])
    }

    private func result(_ kind: Pending, _ body: Data) throws {
        switch kind {
        case .snapshot:
            let s = try Wire.decode(Result<Snapshot>.self, from: body).data
            st.identity = s.identity
            st.presence = s.presence
            st.handoff = s.handoff
            st.responsibilities = s.responsibilities
            st.working = s.activity.working
            st.activity = s.activity                 // rebuilt wholesale, never merged
            haveSnapshot = true
            followHandoff()
        case .sync:
            let page = try Wire.decode(Result<RowsPayload>.self, from: body).data
            mirror.merge(page.rows)
            st.rows = mirror.ordered
            if page.more == true || mirror.hasGap {
                requestSync()
            } else {
                haveSync = true
            }
        case .send(let msgId):
            let r = try Wire.decode(Result<SendResult>.self, from: body).data
            inFlight.remove(msgId)
            setStatus(msgId, .sent(seq: r.seq))
        case .readyOut(let hid):
            settle(hid + "preparing")
        case .ackIn(let hid):
            settle(hid + "committed")
        }
        if haveSnapshot && haveSync && st.connection != .live {
            st.connection = .live
            backoff.reset()
            flushOutbox()
        }
        publish()
    }

    private func failed(_ kind: Pending, code: String, message: String?) {
        switch kind {
        case .send(let msgId):
            inFlight.remove(msgId)
            setStatus(msgId, .rejected(code: code))
            if code == "stale_presence" { request("snapshot", .snapshot) }
        case .readyOut(let hid), .ackIn(let hid):
            st.lastError = "handoff \(hid): \(code)"
            var key = hid + "committed"
            if case .readyOut = kind { key = hid + "preparing" }
            // A fenced refusal is final; anything else may succeed on the next attempt.
            if code == "stale_presence" || code == "handoff_conflict" {
                settle(key)
            } else {
                handoffSent.remove(key)
            }
        case .snapshot, .sync:
            st.lastError = "\(code): \(message ?? "")"
        }
        publish()
    }

    private func event(_ kind: String, _ body: Data) throws {
        switch kind {
        case "messages":
            let rows = try Wire.decode(Event<RowsPayload>.self, from: body).data.rows
            mirror.merge(rows)
            st.rows = mirror.ordered
            if mirror.hasGap { requestSync() }
        case "presence":
            let p = try Wire.decode(Event<PresenceEvent>.self, from: body).data
            st.presence = p.presence
            st.handoff = p.handoff
            followHandoff()
        case "responsibilities":
            st.responsibilities = try Wire.decode(Event<ResponsibilitiesEvent>.self, from: body)
                .data.responsibilities
        case "activity":
            // The snapshot is the base; a push before it, or from another runtime
            // instance than the snapshot's, is ignored.
            let a = try Wire.decode(Event<Activity>.self, from: body).data
            guard haveSnapshot, a.boot == nil || a.boot == st.activity?.boot else { return }
            st.working = a.working
            st.activity = a
        default:
            return
        }
        publish()
    }

    /// Nothing the last runtime reported survives a lost session; the next snapshot is the
    /// only source.
    private func clearActivity() {
        st.working = false
        st.activity = nil
    }

    /// Play this surface's part in an open jack: confirm exit as source, arrival as target.
    private func settle(_ key: String) {
        handoffSent.remove(key)
        handoffDone.insert(key)
    }

    private func followHandoff() {
        guard let h = st.handoff, let p = st.presence else { return }
        let key = h.id + h.state
        guard !handoffSent.contains(key), !handoffDone.contains(key) else { return }
        if h.state == "preparing" && h.from == config.surface && p.surface == config.surface {
            handoffSent.insert(key)
            var fields: [String: Any] = ["handoff_id": h.id, "presence_gen": p.generation]
            if let d = draft(), !d.isEmpty { fields["draft"] = d }
            request("ready_out", .readyOut(h.id), fields)
        } else if h.state == "committed" && h.to == config.surface && p.surface == config.surface {
            handoffSent.insert(key)
            request("ack_in", .ackIn(h.id), ["handoff_id": h.id, "presence_gen": p.generation])
        }
    }

    private func flushOutbox() {
        guard st.connection == .live, let gen = st.presence?.generation else { return }
        for m in st.outbox where m.status == .pending && !inFlight.contains(m.id) {
            inFlight.insert(m.id)
            request("send", .send(m.id), ["conversation_id": Wire.conversation,
                                          "client_msg_id": m.id, "text": m.text,
                                          "presence_gen": m.presenceGenOverride ?? gen])
        }
    }

    private func setStatus(_ id: String, _ status: OutgoingMessage.Status) {
        if let i = st.outbox.firstIndex(where: { $0.id == id }) {
            st.outbox[i].status = status
        }
        // Delivered rows render from the runtime; keep only what still needs attention.
        if st.outbox.count > 20 {
            st.outbox.removeAll { if case .sent = $0.status { true } else { false } }
        }
    }

    private func publish() {
        onChange(st)
    }
}
