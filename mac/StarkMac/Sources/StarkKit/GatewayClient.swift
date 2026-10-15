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

    /// Surface to display name for paired devices, from the snapshot.
    public var deviceNames: [String: String] = [:]

    /// A draft a jack carried here that this surface has not confirmed (confirmDraft).
    public var incomingDraft: CarriedDraft?
    /// The draft this surface sent with its last ready_out, until clearSentDraft.
    public var sentDraft: CarriedDraft?

    public var isActiveSurface: Bool { presence?.surface == surface }

    /// Who holds Stark when this surface does not: a device name, "Mac" for home, or nil.
    public var holderName: String? {
        guard let p = presence, p.surface != surface else { return nil }
        return deviceNames[p.surface] ?? (p.surface == p.home ? "Mac" : p.surface)
    }
    public var isWorking: Bool { activity?.isWorking ?? false }
    public var isThinking: Bool { activity?.isThinking ?? working }

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
    public typealias MakeTransport = @Sendable (String) throws -> Transport

    private enum Pending {
        case snapshot, sync, send(String), readyOut(String), ackIn(String), jack(String),
             draftAck(String)
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

    /// Each connection dials the next host in turn; a paired phone keeps an address and a
    /// Bonjour name, and either may be the one that works on this network.
    private var hostCursor = 0

    public init(config: ClientConfig,
                makeTransport: MakeTransport? = nil,
                onChange: @escaping @Sendable (ClientState) -> Void = { _ in }) {
        self.config = config
        if let makeTransport {
            self.makeTransport = makeTransport
        } else if let device = config.device {
            let c = device.credential
            self.makeTransport = { host in try TLSTransport(host: host, port: c.port, pin: c.pin) }
        } else {
            self.makeTransport = { try SocketTransport(path: $0) }
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

    @discardableResult
    public func send(_ text: String, presenceGen: Int? = nil) -> String {
        let id = "\(config.surface)-" + UUID().uuidString.lowercased()
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
            t = try makeTransport(nextAddress())
        } catch TransportFailure.pinMismatch {
            st.lastError = "the runtime's key does not match this pairing"
            st.connection = .disconnected("untrusted runtime")
            publish()
            scheduleReconnect(id)
            return
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
        draftAcksSent = []
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

    private func nextAddress() -> String {
        guard let hosts = config.device?.credential.hosts, !hosts.isEmpty else {
            return config.socketPath
        }
        defer { hostCursor += 1 }
        return hosts[hostCursor % hosts.count]
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
            st.deviceNames = Dictionary((s.devices ?? []).map { ($0.surface, $0.name) },
                                        uniquingKeysWith: { a, _ in a })
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
            inFlight.remove(msgId)
            setStatus(msgId, .sent(seq: r.seq))
        case .readyOut(let hid):
            settle(hid + "preparing")
        case .ackIn(let hid):
            settle(hid + "committed")
            let r = try Wire.decode(Result<AckInResult>.self, from: body).data
            if let text = r.draft, !text.isEmpty { offer(CarriedDraft(handoffId: hid, text: text)) }
        case .draftAck(let hid):
            draftAcksSent.remove(hid)
            draftAcksOwed.remove(hid)
        case .jack:
            break                                   // the presence push carries the handoff
        }
        if haveSnapshot && haveSync && st.connection != .live {
            st.connection = .live
            backoff.reset()
            flushOutbox()
            sendDraftAcks()
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
        case .draftAck(let hid):
            draftAcksSent.remove(hid)
            // A fenced refusal is final: this surface no longer holds the draft's jack, and
            // the runtime keeps the text recoverable. Anything else retries when live.
            if code == "stale_presence" || code == "invalid" { draftAcksOwed.remove(hid) }
        case .jack(let hid):
            st.lastError = "jack \(hid): \(code)"
            if code == "stale_presence" { request("snapshot", .snapshot) }
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
            if let d = draft(), !d.isEmpty {
                fields["draft"] = d
                st.sentDraft = CarriedDraft(handoffId: h.id, text: d)
            }
            request("ready_out", .readyOut(h.id), fields)
        } else if h.state == "committed" && h.to == config.surface && p.surface == config.surface {
            handoffSent.insert(key)
            request("ack_in", .ackIn(h.id), ["handoff_id": h.id, "presence_gen": p.generation])
        }
    }

    private func offer(_ d: CarriedDraft) {
        guard !draftsConfirmed.contains(d.handoffId) else { return }
        st.incomingDraft = d
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
        onChange(st)
    }
}

/// One time pairing over the pinned LAN gateway. Blocking; call off the main thread.
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
                           makeTransport: ((String) throws -> Transport)? = nil) throws
        -> DeviceCredential
    {
        let dial = makeTransport ?? { try TLSTransport(host: $0, port: link.port, pin: link.pin) }
        var transport: Transport?
        var sawUntrusted = false
        for host in link.candidates {
            do {
                transport = try dial(host)
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
                                agentUUID: p.identity.uuid)
    }
}
