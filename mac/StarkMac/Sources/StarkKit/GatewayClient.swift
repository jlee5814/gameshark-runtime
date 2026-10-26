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

    public init(id: String, text: String, status: Status) {
        self.init(id: id, text: text, status: status, presenceGenOverride: nil)
    }

    init(id: String, text: String, status: Status, presenceGenOverride: Int?) {
        self.id = id
        self.text = text
        self.status = status
        self.presenceGenOverride = presenceGenOverride
    }
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

    /// Surface to display name for paired devices, from the snapshot.
    public var deviceNames: [String: String] = [:]

    /// A draft a jack carried here that this surface has not confirmed (confirmDraft).
    public var incomingDraft: CarriedDraft?
    /// The draft the runtime confirmed reached the jack's target: draft_carried on an
    /// acked jack. Until clearSentDraft. Sending it, or a commit the target never acked,
    /// is not enough: only this receipt lets a source clear its composer.
    public var sentDraft: CarriedDraft?

    public var isActiveSurface: Bool { presence?.surface == surface }

    /// Who holds Stark when this surface does not: a device name, "Mac" for home, or nil.
    public var holderName: String? {
        guard let p = presence, p.surface != surface else { return nil }
        return deviceNames[p.surface] ?? (p.surface == p.home ? "Mac" : p.surface)
    }
    /// The model behind replies, from the last snapshot.
    public var model: ModelInfo?
    public var isWorking: Bool { activity?.isWorking ?? false }
    /// What the runtime is doing with the newest user message; nil once answered.
    public var reply: ReplyState? { activity?.reply }
    /// Only the surface holding Stark, live, may ask for a failed reply again.
    public var canRetryReply: Bool {
        connection.isLive && isActiveSurface && reply?.failed == true
    }
    public var isThinking: Bool { activity?.isThinking ?? working }

    /// The runtime serves memory commands (its snapshot lists the feature).
    public var memoryAvailable = false
    /// Saved memories, from memory_list and memories pushes: the runtime's list, never a
    /// local copy that could drift from it.
    public var memories: [MemoryItem] = []
    /// Whether saved memories reach the model; the operator can revoke it on the Mac.
    public var memoryInject = true
    /// Memory writes not yet answered, and refused ones until discarded.
    public var memoryOps: [MemoryOp] = []
    /// The last memory outcome worth saying, such as what a forget left in history.
    public var memoryNotice: String?
    /// The runtime serves loadout_set (its snapshot lists the feature).
    public var loadoutAvailable = false
    /// What Stark wears, from the snapshot and loadout pushes. Kept while disconnected, so
    /// a dropped session never strips his cap; a new identity resets it.
    public var loadout: LoadoutState = .standard
    /// A manual pin not yet answered. It shows at once and goes out when live.
    public var loadoutOp: LoadoutOp?
    /// What the avatar draws: the pending pin if any, else the runtime's value.
    public var shownLoadout: Loadout {
        guard let op = loadoutOp else { return loadout.effective }
        return op.pin ?? loadout.inferred
    }

    /// Publish order, rising across every client in the process: a consumer that receives
    /// states out of order (a Task hop) can tell a late one from news. 0 before any publish.
    public var revision = 0

    /// Remember and correct need presence, as a chat send does.
    public var canWriteMemory: Bool { connection.isLive && isActiveSurface && memoryAvailable }
    /// Forget needs only a live session: deleting is never blocked by where Stark is.
    public var canForgetMemory: Bool { connection.isLive && memoryAvailable }

    public init(surface: String) {
        self.surface = surface
    }
}

/// A paired device's way in: pinned TLS to the LAN gateway, then an ECDSA proof.
public struct DeviceEndpoint: Sendable {
    public var credential: DeviceCredential
    public var signer: any DeviceSigner

    public init(credential: DeviceCredential, signer: any DeviceSigner) {
        self.credential = credential
        self.signer = signer
    }
}

public struct ClientConfig: Sendable {
    public var socketPath: String
    public var tokenURL: URL
    public var surface: String
    public var backoff: Backoff
    /// Set for a paired device; nil means the Unix socket and token (StarkMac).
    public var device: DeviceEndpoint?
    public var clientName = "StarkMac/0.4"
    /// Connection lifecycle lines (path, dial, state, retry, send). Never secrets.
    public var trace: Trace?

    public init(socketPath: String, tokenURL: URL, surface: String = "mac",
                backoff: Backoff = Backoff()) {
        self.socketPath = socketPath
        self.tokenURL = tokenURL
        self.surface = surface
        self.backoff = backoff
    }

    /// A paired device. Its surface comes from the pairing, and the runtime binds it again
    /// from its own device row at every hello.
    public init(device: DeviceEndpoint, backoff: Backoff = Backoff(), clientName: String) {
        self.socketPath = ""
        self.tokenURL = URL(fileURLWithPath: "/dev/null")
        self.surface = device.credential.surface
        self.backoff = backoff
        self.device = device
        self.clientName = clientName
    }

    /// Same rule as stark.py: socket from STARK_SOCKET, else ~/.gameshark/gateway/stark.sock
    /// (the private directory beside the default database); token from STARK_TOKEN, else
    /// gateway.token beside the socket.
    public static func fromEnvironment(_ env: [String: String] = ProcessInfo.processInfo.environment)
        -> ClientConfig
    {
        #if os(macOS)
        let home = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".gameshark")
        #else
        let home = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent(".gameshark")
        #endif
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
    /// Dials one route: a TLS host and port for a device, the socket path (as the host)
    /// for StarkMac.
    public typealias MakeTransport = @Sendable (Route) throws -> Transport

    private enum Pending {
        case snapshot, sync, send(String), readyOut(String), ackIn(String), jack(String),
             draftAck(String), retryReply(Int), memoryList, memory(String), loadout(String)
    }

    public enum JackDirection: String, Sendable {
        /// Pull presence to this surface (Jack In).
        case here = "self"
        /// Send Stark home from this surface (Jack Out).
        case home
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
    /// Sends for the current connection, in order, off the client's queue: a socket that
    /// stops draining never holds a network change, a reconnect, or stop().
    private var writer = DispatchQueue(label: "stark.gateway.writer")
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
    /// Drafts the UI confirmed. Kept across reconnects, so one is never offered again.
    private var draftsConfirmed: Set<String> = []
    /// Confirmed drafts whose receipt the runtime has not answered; resent when live.
    private var draftAcksOwed: Set<String> = []
    /// draft_ack requests sent on this connection and awaiting a result.
    private var draftAcksSent: Set<String> = []
    /// A Retry the runtime has not answered, by seq; resent when live, since a reconnect
    /// forgets pending requests. The runtime keys retries by seq, so a repeat is harmless.
    private var retryOwed: Int?
    /// retry_reply sent on this connection and awaiting a result.
    private var retrySent = false
    /// The draft the last ready_out carried, until the runtime answers whether the
    /// committed jack holds it. Kept across reconnects: a lost reply is reconciled by
    /// replaying the same ready_out, which the runtime answers from the durable row.
    private var draftAttempt: DraftAttempt?
    /// Memory ops sent on this connection and awaiting a result. Cleared on reconnect, so
    /// an op lost with its connection is sent again under the same op id.
    private var memorySent: Set<String> = []
    private var loadoutSent = false
    /// memory_list asked on this connection.
    private var memoryListed = false

    private struct DraftAttempt {
        var draft: CarriedDraft
        var presenceGen: Int
        /// The runtime committed the jack with this text; its target has not acked yet,
        /// and a revert would leave the text nowhere. Asked again once the jack closes.
        var committed = false
    }

    /// Each connection dials the next route in turn; a paired phone keeps a LAN address, a
    /// Bonjour name, and its Mac's tailnet address and name, and any may be the one that
    /// works on this network. The order follows the network the phone is on.
    private var hostCursor = 0
    /// The cursor of the route being dialed; a welcome on it makes it the next one too, so
    /// a reconnect on the same network skips routes that already failed there.
    private var dialing = 0
    private var path: NetworkPath?

    public init(config: ClientConfig,
                makeTransport: MakeTransport? = nil,
                onChange: @escaping @Sendable (ClientState) -> Void = { _ in }) {
        self.config = config
        if let makeTransport {
            self.makeTransport = makeTransport
        } else if let device = config.device {
            let pin = device.credential.pin
            let trace = config.trace
            self.makeTransport = { r in
                try TLSTransport(host: r.host, port: r.port, pin: pin, trace: trace)
            }
        } else {
            self.makeTransport = { try SocketTransport(path: $0.host) }
        }
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

    /// The phone's network changed (NWPathMonitor). Routes are reordered for it; a move to
    /// another class of network (Wi-Fi to cellular, offline to online) also redials at
    /// once, since the old socket rides an interface that may be gone. Messages resend
    /// under their original ids and rows resync by seq, so nothing lands twice.
    public func setPath(_ p: NetworkPath) {
        trace("path requested \(p.rawValue)")
        queue.async {
            let old = self.path
            self.path = p
            let redial = old != p && old != nil && p != .none && self.running
            self.trace("path \(old?.rawValue ?? "nil") -> \(p.rawValue) running=\(self.running) "
                       + "conn=\(self.st.connection) redial=\(redial)")
            guard old != p else { return }
            self.hostCursor = 0
            guard old != nil, p != .none, self.running else { return }
            self.drop(reason: "network changed", reconnect: false)
            self.backoff.reset()
            self.connect()
        }
    }

    /// Supplies the unsent draft when this surface is asked to jack out.
    public func setDraftProvider(_ provider: @escaping @Sendable () -> String?) {
        queue.async { self.draft = provider }
    }

    /// The UI put an incoming draft in its composer, or the user chose to keep their own
    /// text: tell the runtime, which then stops offering it. Retried across reconnects.
    public func confirmDraft(_ handoffId: String) {
        queue.async {
            guard !self.draftsConfirmed.contains(handoffId) else { return }
            self.draftsConfirmed.insert(handoffId)
            self.draftAcksOwed.insert(handoffId)
            if self.st.incomingDraft?.handoffId == handoffId { self.st.incomingDraft = nil }
            self.sendDraftAcks()
            self.publish()
        }
    }

    /// The UI dealt with the draft this surface sent out (cleared or kept its composer).
    public func clearSentDraft(_ handoffId: String) {
        queue.async {
            guard self.st.sentDraft?.handoffId == handoffId else { return }
            self.st.sentDraft = nil
            self.publish()
        }
    }

    /// Queue a user message. presenceGen overrides the held generation, which exists only to
    /// demonstrate that the runtime refuses an obsolete one.
    /// Jack In (here) or Jack Out (home). The runtime opens the two phase handoff; this
    /// client then plays its part (ready_out as source, ack_in as target) from the pushes.
    @discardableResult
    public func jack(_ to: JackDirection) -> String {
        let hid = "\(config.surface)-jack-" + UUID().uuidString.lowercased().prefix(13)
        queue.async {
            guard self.st.connection == .live, let gen = self.st.presence?.generation else {
                self.st.lastError = "jack: not connected"
                self.publish()
                return
            }
            self.request("jack", .jack(hid), ["handoff_id": hid, "to": to.rawValue,
                                             "presence_gen": gen])
        }
        return hid
    }

    /// Ask the runtime to answer a failed reply again. The runtime keys the retry by seq,
    /// so a repeat while the first is pending, or after it lands, starts nothing new.
    public func retryReply(seq: Int) {
        queue.async {
            guard self.st.connection == .live, self.st.presence != nil else {
                self.st.lastError = "retry: not connected"
                self.publish()
                return
            }
            self.retryOwed = seq
            self.retrySent = false
            self.sendRetry()
        }
    }

    /// Save a fact the user typed as an explicit command. Returns the op id.
    @discardableResult
    public func remember(_ text: String) -> String {
        enqueueMemory(.remember(text: text))
    }

    /// Replace a memory's text. revision is the one the user edited; a newer one elsewhere
    /// makes the runtime refuse with conflict instead of overwriting it.
    @discardableResult
    public func correctMemory(id: String, revision: Int, text: String) -> String {
        enqueueMemory(.correct(memoryId: id, revision: revision, text: text))
    }

    /// Forget a memory. Allowed from any live surface, wherever Stark is.
    @discardableResult
    public func forgetMemory(id: String) -> String {
        enqueueMemory(.forget(memoryId: id))
    }

    /// Drop a refused memory op from the list, and the notice with it.
    public func discardMemoryOp(_ opId: String) {
        queue.async {
            self.st.memoryOps.removeAll { $0.id == opId && $0.status != .pending }
            self.st.memoryNotice = nil
            self.publish()
        }
    }

    public func dismissMemoryNotice() {
        queue.async {
            self.st.memoryNotice = nil
            self.publish()
        }
    }

    /// Pin a loadout, or clear the pin with nil (auto). Any live surface may, wherever Stark
    /// is. Returns the op id; a newer call replaces an op not yet sent.
    @discardableResult
    public func setLoadout(_ pin: Loadout?) -> String {
        let id = "\(config.surface)-lo-" + UUID().uuidString.lowercased()
        queue.async {
            self.st.loadoutOp = LoadoutOp(id: id, pin: pin)
            self.loadoutSent = false
            self.flushLoadout()
            self.publish()
        }
        return id
    }

    private func enqueueMemory(_ kind: MemoryOp.Kind) -> String {
        let id = "\(config.surface)-mem-" + UUID().uuidString.lowercased()
        queue.async {
            self.st.memoryOps.append(MemoryOp(id: id, kind: kind, status: .pending))
            self.st.memoryNotice = nil
            self.flushMemoryOps()
            self.publish()
        }
        return id
    }

    @discardableResult
    public func send(_ text: String, presenceGen: Int? = nil) -> String {
        let id = "\(config.surface)-" + UUID().uuidString.lowercased()
        // Logged off the queue and again on it: a gap between the two is a stalled queue.
        trace("send requested msg=\(id)")
        queue.async {
            self.trace("send queued msg=\(id) conn=\(self.st.connection)")
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
        let route = nextRoute()
        trace("dial #\(id) \(route.via.rawValue) \(route.host):\(route.port) path="
              + "\(path?.rawValue ?? "nil") cursor=\(dialing)")
        do {
            t = try makeTransport(route)
            trace("dial #\(id) connected")
        } catch TransportFailure.pinMismatch {
            trace("dial #\(id) pin mismatch")
            st.lastError = "the runtime's key does not match this pairing"
            st.connection = .disconnected("untrusted runtime")
            publish()
            scheduleReconnect(id)
            return
        } catch {
            trace("dial #\(id) failed: \(error)")
            st.connection = .disconnected("runtime unreachable")
            publish()
            scheduleReconnect(id)
            return
        }
        transport = t
        writer = DispatchQueue(label: "stark.gateway.writer")
        reader = FrameReader()
        pending = [:]
        inFlight = []
        handoffSent = []
        draftAcksSent = []
        retrySent = false
        memorySent = []
        memoryListed = false
        loadoutSent = false
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

    private func nextRoute() -> Route {
        guard let routes = config.device?.credential.routes, !routes.isEmpty else {
            return Route(host: config.socketPath, port: 0, via: .lan)
        }
        let ordered = Route.ordered(routes, for: path ?? .none)
        dialing = hostCursor % ordered.count
        hostCursor = dialing + 1
        return ordered[dialing]
    }

    private func drop(reason: String, reconnect: Bool) {
        trace("drop \(reason) reconnect=\(reconnect) had=\(transport != nil)")
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
        trace("retry in \(String(format: "%.2f", delay))s")
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
        let what = "\(object["type"] ?? "?") id=\(object["id"] ?? "-")"
            + (object["client_msg_id"].map { " msg=\($0)" } ?? "")
        let frame: Data
        do {
            frame = try Frame.encode(json: object)
        } catch {
            drop(reason: "write failed", reconnect: true)
            return
        }
        trace("write \(what)")
        let id = connId
        writer.async { [weak self] in
            do {
                try t.send(frame)
                self?.trace("wrote \(what)")
            } catch {
                // A send released by drop() belongs to an older connection: ignored.
                guard let self else { return }
                self.queue.async {
                    guard id == self.connId else { return }
                    self.drop(reason: "write failed", reconnect: true)
                }
            }
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
            if let device = config.device {
                helloAsDevice(device, nonce: nonce, serverFP: c.serverFp)
                return
            }
            let token: Data
            do { token = try Auth.loadToken(at: config.tokenURL) } catch {
                st.lastError = "token: \(error)"
                drop(reason: "no token", reconnect: true)
                return
            }
            write(["type": "hello", "proto": Wire.protocolVersion, "surface": config.surface,
                   "client": config.clientName,
                   "mac": Auth.sign(token: token, nonce: nonce, surface: config.surface,
                                    proto: Wire.protocolVersion)])
        case "welcome":
            let w = try Wire.decode(Welcome.self, from: body)
            if w.surface != config.surface {
                st.lastError = "runtime bound surface \(w.surface), expected \(config.surface)"
                drop(reason: "surface mismatch", reconnect: false)
                return
            }
            if let device = config.device, w.identity.uuid != device.credential.agentUUID {
                // The pin matched, but another database answers (runtimes sharing a key):
                // never show a second agent in this view. Try the next route.
                st.lastError = "another agent answered (\(w.identity.uuid)); this iPhone "
                    + "is paired with \(device.credential.agentUUID)"
                drop(reason: "another agent", reconnect: true)
                return
            }
            if let known = st.identity, known.uuid != w.identity.uuid {
                // Another runtime database: nothing cached belongs to it.
                mirror.reset()
                st.rows = []
                // Memories belong to an identity: a write queued for the old runtime is
                // never sent to this one, pending or not.
                st.memories = []
                st.memoryOps = []
                memorySent = []
                st.loadout = .standard
                st.loadoutOp = nil
                loadoutSent = false
                st.outbox.removeAll { if case .pending = $0.status { false } else { true } }
            }
            st.identity = w.identity
            trace("welcome on cursor \(dialing)")
            hostCursor = dialing
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
            st.model = s.model
            st.deviceNames = Dictionary((s.devices ?? []).map { ($0.surface, $0.name) },
                                        uniquingKeysWith: { a, _ in a })
            st.memoryAvailable = s.features?.contains("memory") == true
            if let m = s.memory { st.memoryInject = m.inject }
            st.loadoutAvailable = s.features?.contains("loadout") == true
            st.loadout = s.loadout ?? .standard
            if st.memoryAvailable && !memoryListed {
                memoryListed = true
                request("memory_list", .memoryList)
            }
            if let d = s.draft {
                offer(d)
            } else if let d = st.incomingDraft, !draftAcksOwed.contains(d.handoffId) {
                st.incomingDraft = nil              // the runtime no longer offers it here
            }
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
            trace("sent msg=\(msgId) seq=\(r.seq)")
            inFlight.remove(msgId)
            setStatus(msgId, .sent(seq: r.seq))
        case .readyOut(let hid):
            settle(hid + "preparing")
            let r = try Wire.decode(Result<ReadyOutResult>.self, from: body).data
            if var a = draftAttempt, a.draft.handoffId == hid {
                // Acked with exactly this text: the target holds it. Committed: ask again
                // when the jack closes. Anything else (a deadline commit, a revert, an
                // older runtime) leaves the composer alone.
                if r.draftCarried == true && r.state == "acked" {
                    draftAttempt = nil
                    st.sentDraft = a.draft
                } else if r.draftCarried == true && r.state == "committed" {
                    a.committed = true
                    draftAttempt = a
                } else {
                    draftAttempt = nil
                }
            }
        case .ackIn(let hid):
            settle(hid + "committed")
            let r = try Wire.decode(Result<AckInResult>.self, from: body).data
            if let text = r.draft, !text.isEmpty { offer(CarriedDraft(handoffId: hid, text: text)) }
        case .draftAck(let hid):
            draftAcksSent.remove(hid)
            draftAcksOwed.remove(hid)
        case .jack:
            break                                   // the presence push carries the handoff
        case .retryReply(let seq):
            if retryOwed == seq { retryOwed = nil }
            retrySent = false
            let r = try Wire.decode(Result<RetryResult>.self, from: body).data
            trace("retry seq=\(seq) r\(r.retry) replayed=\(r.replayed)")
        case .memoryList:
            let p = try Wire.decode(Result<MemoriesPayload>.self, from: body).data
            st.memories = p.memories
            st.memoryInject = p.inject
        case .memory(let opId):
            memorySent.remove(opId)
            guard let op = st.memoryOps.first(where: { $0.id == opId }) else { break }
            st.memoryOps.removeAll { $0.id == opId }
            if case .forget(let mid) = op.kind {
                let r = try Wire.decode(Result<ForgetResult>.self, from: body).data
                st.memories.removeAll { $0.id == mid }
                st.memoryNotice = MemoryText.forgotten(historyMentions: r.historyMentions)
            } else {
                let m = try Wire.decode(Result<MemoryWriteResult>.self, from: body).data.memory
                if let i = st.memories.firstIndex(where: { $0.id == m.id }) {
                    st.memories[i] = m
                } else {
                    st.memories.append(m)
                }
            }
        case .loadout(let opId):
            let r = try Wire.decode(Result<LoadoutSetResult>.self, from: body).data
            st.loadout = r.loadout
            if st.loadoutOp?.id == opId {
                st.loadoutOp = nil
                loadoutSent = false
            }
        }
        if haveSnapshot && haveSync && st.connection != .live {
            st.connection = .live
            trace("live")
            backoff.reset()
            flushOutbox()
            sendDraftAcks()
            sendRetry()
            flushMemoryOps()
            flushLoadout()
        }
        publish()
    }

    private func failed(_ kind: Pending, code: String, message: String?) {
        switch kind {
        case .send(let msgId):
            trace("send msg=\(msgId) refused: \(code)")
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
                if draftAttempt?.draft.handoffId == hid { draftAttempt = nil }   // keep it
            } else {
                handoffSent.remove(key)
            }
        case .draftAck(let hid):
            draftAcksSent.remove(hid)
            // A fenced refusal is final: this surface no longer holds the draft's jack, and
            // the runtime keeps the text recoverable. Anything else retries when live.
            if code == "stale_presence" || code == "invalid" { draftAcksOwed.remove(hid) }
        case .jack(let hid):
            st.lastError = "jack \(hid): \(code)"
            if code == "stale_presence" { request("snapshot", .snapshot) }
        case .retryReply(let seq):
            st.lastError = "retry \(seq): \(code)"
            retrySent = false
            // A fenced or invalid refusal is final; anything else retries when live.
            if code == "stale_presence" || code == "invalid", retryOwed == seq { retryOwed = nil }
            if code == "stale_presence" { request("snapshot", .snapshot) }
        case .memoryList:
            st.lastError = "memory_list: \(code)"
        case .memory(let opId):
            memorySent.remove(opId)
            guard let i = st.memoryOps.firstIndex(where: { $0.id == opId }) else { break }
            if case .forget(let mid) = st.memoryOps[i].kind, code == "invalid" {
                // Already gone from the runtime: nothing is left to forget.
                st.memoryOps.remove(at: i)
                st.memories.removeAll { $0.id == mid }
                break
            }
            // The runtime lost or is handing over ownership and wrote nothing: the op stays
            // pending and goes again, under the same op id, once a session is live.
            if code == "fenced" || code == "draining" { break }
            if code == "superseded" {
                // It landed on an earlier attempt and the memory changed since: settled.
                st.memoryOps.remove(at: i)
                request("memory_list", .memoryList)
                break
            }
            // Any other refusal is final for this op; the user decides whether to try again.
            st.memoryOps[i].status = .rejected(code: code)
            st.memoryNotice = MemoryText.refused(code)
            if code == "stale_presence" { request("snapshot", .snapshot) }
            if code == "conflict" { request("memory_list", .memoryList) }
        case .loadout(let opId):
            guard st.loadoutOp?.id == opId else { break }
            loadoutSent = false
            // Ownership moved mid write and nothing landed: send again once live.
            if code == "fenced" || code == "draining" { break }
            st.loadoutOp = nil
            st.lastError = "loadout: \(code)"
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
            if p.presence.surface != config.surface { st.incomingDraft = nil }   // stale now
            followHandoff()
            let holder = p.presence.surface
            if holder != p.presence.home, holder != config.surface, st.deviceNames[holder] == nil {
                request("snapshot", .snapshot)      // a device paired since our snapshot
            }
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
        case "memories":
            let p = try Wire.decode(Event<MemoriesPayload>.self, from: body).data
            st.memories = p.memories
            st.memoryInject = p.inject
        case "loadout":
            st.loadout = try Wire.decode(Event<LoadoutState>.self, from: body).data
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
        reconcileDraft()
        guard let h = st.handoff, let p = st.presence else { return }
        let key = h.id + h.state
        guard !handoffSent.contains(key), !handoffDone.contains(key) else { return }
        if h.state == "preparing" && h.from == config.surface && p.surface == config.surface {
            handoffSent.insert(key)
            var fields: [String: Any] = ["handoff_id": h.id, "presence_gen": p.generation]
            if let d = draft(), !d.isEmpty {
                fields["draft"] = d
                draftAttempt = DraftAttempt(draft: CarriedDraft(handoffId: h.id, text: d),
                                            presenceGen: p.generation)
            }
            request("ready_out", .readyOut(h.id), fields)
        } else if h.state == "committed" && h.to == config.surface && p.surface == config.surface {
            handoffSent.insert(key)
            request("ack_in", .ackIn(h.id), ["handoff_id": h.id, "presence_gen": p.generation])
        }
    }

    /// A ready_out that carried a draft lost its answer and its jack moved past preparing,
    /// or its jack committed and has since closed: ask again with the same text and
    /// generation. The runtime answers a replay from the handoff row without changing it.
    private func reconcileDraft() {
        guard let a = draftAttempt, haveSnapshot else { return }
        let hid = a.draft.handoffId
        let key = hid + "preparing"
        guard !handoffSent.contains(key) else { return }
        if let h = st.handoff, h.id == hid {
            if h.state == "preparing" { return }            // the normal path sends it
            if a.committed { return }                       // still open: wait for the close
        }
        handoffSent.insert(key)
        request("ready_out", .readyOut(hid), ["handoff_id": hid, "presence_gen": a.presenceGen,
                                              "draft": a.draft.text])
    }

    private func offer(_ d: CarriedDraft) {
        guard !draftsConfirmed.contains(d.handoffId) else { return }
        st.incomingDraft = d
    }

    private func sendRetry() {
        guard st.connection == .live, !retrySent, let seq = retryOwed,
              let gen = st.presence?.generation else { return }
        retrySent = true
        request("retry_reply", .retryReply(seq), ["conversation_id": Wire.conversation,
                                                  "seq": seq, "presence_gen": gen])
    }

    private func sendDraftAcks() {
        guard st.connection == .live, let gen = st.presence?.generation else { return }
        for hid in draftAcksOwed.subtracting(draftAcksSent).sorted() {
            draftAcksSent.insert(hid)
            request("draft_ack", .draftAck(hid), ["handoff_id": hid, "presence_gen": gen])
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

    private func flushMemoryOps() {
        guard st.connection == .live, st.memoryAvailable,
              let gen = st.presence?.generation else { return }
        for op in st.memoryOps where op.status == .pending && !memorySent.contains(op.id) {
            memorySent.insert(op.id)
            switch op.kind {
            case .remember(let text):
                request("memory_remember", .memory(op.id), ["op_id": op.id, "text": text,
                                                           "presence_gen": gen])
            case .correct(let mid, let rev, let text):
                request("memory_correct", .memory(op.id), ["op_id": op.id, "memory_id": mid,
                                                          "revision": rev, "text": text,
                                                          "presence_gen": gen])
            case .forget(let mid):
                request("memory_forget", .memory(op.id), ["op_id": op.id, "memory_id": mid])
            }
        }
    }

    private func flushLoadout() {
        guard st.connection == .live, st.loadoutAvailable, !loadoutSent,
              let op = st.loadoutOp else { return }
        loadoutSent = true
        request("loadout_set", .loadout(op.id),
                ["op_id": op.id, "override": op.pin?.rawValue ?? NSNull()])
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

    private func helloAsDevice(_ device: DeviceEndpoint, nonce: Data, serverFP: String?) {
        let c = device.credential
        // The TLS layer already enforced the pin; the challenge must name the same key.
        guard let fpHex = serverFP, Data(hex: fpHex) == c.pin else {
            st.lastError = "challenge names another server key"
            drop(reason: "untrusted runtime", reconnect: true)
            return
        }
        let message = DeviceAuth.transcript(nonce: nonce, deviceID: c.deviceID,
                                            proto: Wire.protocolVersion, serverFP: c.pin)
        guard let sig = try? device.signer.sign(message) else {
            st.lastError = "device key unavailable"
            drop(reason: "no device key", reconnect: true)
            return
        }
        write(["type": "hello", "proto": Wire.protocolVersion, "device_id": c.deviceID,
               "sig": sig.hex, "client": config.clientName])
    }

    private func publish() {
        st.revision = Revision.next()
        onChange(st)
    }

    private func trace(_ line: @autoclosure () -> String) {
        config.trace?(line())
    }
}

/// One time pairing over the pinned LAN gateway. Blocking; call off the main thread.
/// The process wide publish counter behind ClientState.revision.
enum Revision {
    private static let lock = NSLock()
    nonisolated(unsafe) private static var value = 0

    static func next() -> Int {
        lock.withLock {
            value += 1
            return value
        }
    }
}

public enum Pairing {
    public enum Failure: Error, Equatable {
        case unreachable
        case untrusted
        case refused(String)
        case protocolError(String)
    }

    struct Paired: Decodable {
        var deviceId: String
        var surface: String
        var identity: Identity
    }

    /// Dial each candidate address, present the link's secret and the signer's public key,
    /// and return the credential to store. A used, expired, or wrong link is refused.
    public static func run(link: PairingLink, name: String, signer: any DeviceSigner,
                           makeTransport: ((Route) throws -> Transport)? = nil) throws
        -> DeviceCredential
    {
        let dial = makeTransport ?? { try TLSTransport(host: $0.host, port: $0.port, pin: link.pin) }
        var transport: Transport?
        var sawUntrusted = false
        for route in link.routes {
            do {
                transport = try dial(route)
                break
            } catch TransportFailure.pinMismatch {
                sawUntrusted = true
            } catch {
                continue
            }
        }
        guard let t = transport else { throw sawUntrusted ? Failure.untrusted : Failure.unreachable }
        defer { t.close() }
        var reader = FrameReader()
        func next() throws -> Data {
            while true {
                if let body = try reader.next() { return body }
                let bytes = try t.receive()
                if bytes.isEmpty { throw Failure.protocolError("runtime closed the connection") }
                reader.feed(bytes)
            }
        }
        let challenge = try Wire.decode(Challenge.self, from: next())
        guard challenge.proto.contains(Wire.protocolVersion),
              challenge.serverFp.flatMap({ Data(hex: $0) }) == link.pin
        else { throw Failure.protocolError("unexpected challenge") }
        try t.send(Frame.encode(json: [
            "type": "pair", "proto": Wire.protocolVersion, "pairing_id": link.pairingID,
            "secret": link.secret, "name": name, "public_key": signer.publicKey.hex]))
        let body = try next()
        let head = try Wire.decode(Head.self, from: body)
        if head.type == "error" {
            throw Failure.refused(try Wire.decode(ErrorFrame.self, from: body).code)
        }
        guard head.type == "paired" else { throw Failure.protocolError("expected paired") }
        let p = try Wire.decode(Paired.self, from: body)
        return DeviceCredential(deviceID: p.deviceId, surface: p.surface, name: name,
                                hosts: link.candidates, port: link.port, pin: link.pin,
                                agentUUID: p.identity.uuid, routes: link.routes)
    }
}
