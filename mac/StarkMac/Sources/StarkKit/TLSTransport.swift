import CryptoKit
import Foundation
import Network
import Security

/// TLS 1.3 over TCP to the runtime's LAN gateway (gameshark/net_gateway.py), trusting
/// exactly one server key: the pin from the pairing link. No certificate authority, host
/// name, or expiry is consulted; a different key fails the handshake before any frame.
/// Blocking calls over Network.framework, matching the Transport protocol.
public final class TLSTransport: Transport, @unchecked Sendable {
    private let conn: NWConnection
    private let queue = DispatchQueue(label: "stark.tls")
    private let lock = NSLock()
    private var isClosed = false
    private let pinRejected = Box(false)
    private let trace: Trace?
    private let label: String

    /// `trace` sees every connection state with its elapsed time, so a device log tells a
    /// dial that never left (waiting), a TCP failure, a TLS failure, and a timeout apart.
    public init(host: String, port: UInt16, pin: Data, timeout: TimeInterval = 5,
                trace: Trace? = nil) throws {
        self.trace = trace
        self.label = "\(host):\(port)"
        let started = DispatchTime.now()
        @Sendable func ms() -> Int { Int((DispatchTime.now().uptimeNanoseconds - started.uptimeNanoseconds) / 1_000_000) }
        let tls = NWProtocolTLS.Options()
        let sec = tls.securityProtocolOptions
        sec_protocol_options_set_min_tls_protocol_version(sec, .TLSv13)
        let tcp = NWProtocolTCP.Options()
        tcp.noDelay = true
        tcp.connectionTimeout = Int(max(1, timeout.rounded(.up)))
        guard let nwPort = NWEndpoint.Port(rawValue: port) else { throw TransportFailure.connect(EINVAL) }
        // Install the pin check before the parameters copy these options.
        let rejected = pinRejected
        sec_protocol_options_set_verify_block(sec, { _, trust, complete in
            let ok = TLSTransport.matches(trust: sec_trust_copy_ref(trust).takeRetainedValue(),
                                          pin: pin)
            if !ok { rejected.value = true }
            complete(ok)
        }, queue)
        conn = NWConnection(host: NWEndpoint.Host(host), port: nwPort,
                            using: NWParameters(tls: tls, tcp: tcp))

        let ready = DispatchSemaphore(value: 0)
        let outcome = Box<Error?>(nil)
        let label = self.label
        conn.stateUpdateHandler = { state in
            trace?("tls \(label) +\(ms())ms \(TLSTransport.describe(state))")
            switch state {
            case .ready:
                ready.signal()
            case .failed(let e), .waiting(let e):
                outcome.value = e
                ready.signal()
            case .cancelled:
                ready.signal()
            default:
                break
            }
        }
        conn.start(queue: queue)
        let timedOut = ready.wait(timeout: .now() + timeout) == .timedOut
        conn.stateUpdateHandler = nil
        if timedOut || outcome.value != nil || conn.state != .ready {
            conn.cancel()
            trace?("tls \(label) +\(ms())ms gave up: "
                   + (pinMismatch ? "pin mismatch" : timedOut ? "timeout" : "not ready"))
            if pinMismatch { throw TransportFailure.pinMismatch }
            throw TransportFailure.connect(timedOut ? ETIMEDOUT : ECONNREFUSED)
        }
        if let trace {
            // After ready: what happens to a live session when its interface goes away.
            conn.stateUpdateHandler = { trace("tls \(label) live \(TLSTransport.describe($0))") }
            conn.viabilityUpdateHandler = { trace("tls \(label) viable=\($0)") }
            conn.betterPathUpdateHandler = { trace("tls \(label) betterPath=\($0)") }
            let p = conn.currentPath
            trace("tls \(label) ready +\(ms())ms via "
                  + (p?.availableInterfaces.map { "\($0.name)/\($0.type)" }.joined(separator: ",")
                     ?? "?"))
        }
    }

    /// A state with its error domain: posix is TCP (refused, unreachable, timed out), tls is
    /// the handshake, dns is name resolution.
    static func describe(_ state: NWConnection.State) -> String {
        func err(_ e: NWError) -> String {
            switch e {
            case .posix(let c): "posix \(c.rawValue) \(c)"
            case .tls(let s): "tls \(s)"
            case .dns(let d): "dns \(d)"
            default: "\(e)"
            }
        }
        switch state {
        case .setup: return "setup"
        case .preparing: return "preparing"
        case .ready: return "ready"
        case .cancelled: return "cancelled"
        case .waiting(let e): return "waiting(\(err(e)))"
        case .failed(let e): return "failed(\(err(e)))"
        @unknown default: return "unknown"
        }
    }

    /// Leaf certificate's public key, X9.63 bytes, hashed and compared to the pin.
    static func matches(trust: SecTrust, pin: Data) -> Bool {
        guard let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate],
              let leaf = chain.first, let key = SecCertificateCopyKey(leaf),
              let raw = SecKeyCopyExternalRepresentation(key, nil) as Data?
        else { return false }
        return DeviceAuth.pin(ofPublicKey: raw) == pin
    }

    /// Set by the verify block on the transport's queue before the state change that
    /// releases init, so reading it after the semaphore is ordered.
    public var pinMismatch: Bool { pinRejected.value }

    public func send(_ frame: Data) throws {
        if closed { throw TransportFailure.closed }
        let done = DispatchSemaphore(value: 0)
        let failure = Box<NWError?>(nil)
        conn.send(content: frame, completion: .contentProcessed { e in
            failure.value = e
            done.signal()
        })
        if let trace, done.wait(timeout: .now() + 2) == .timedOut {
            trace("tls \(label) send of \(frame.count) bytes still waiting after 2s")
            done.wait()
            trace("tls \(label) send finished late")
        } else if trace == nil {
            done.wait()
        }
        if let e = failure.value { trace?("tls \(label) send failed: \(e)") }
        if failure.value != nil { throw TransportFailure.io(EIO) }
    }

    public func receive() throws -> Data {
        if closed { throw TransportFailure.closed }
        let done = DispatchSemaphore(value: 0)
        let result = Box<(Data?, Bool, NWError?)>((nil, false, nil))
        conn.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) { data, _, complete, e in
            result.value = (data, complete, e)
            done.signal()
        }
        done.wait()
        let (data, complete, e) = result.value
        if let data, !data.isEmpty { return data }
        trace?("tls \(label) receive ended: complete=\(complete) error=\(e.map { "\($0)" } ?? "none")")
        if complete { return Data() }
        if closed { throw TransportFailure.closed }
        if e != nil { throw TransportFailure.io(EIO) }
        return Data()
    }

    private var closed: Bool { lock.withLock { isClosed } }

    public func close() {
        let already = lock.withLock { () -> Bool in
            defer { isClosed = true }
            return isClosed
        }
        guard !already else { return }
        conn.cancel()
    }

    deinit { close() }
}

/// A mutable cell handed to a completion handler and read after its semaphore fires.
final class Box<T>: @unchecked Sendable {
    var value: T
    init(_ value: T) { self.value = value }
}
