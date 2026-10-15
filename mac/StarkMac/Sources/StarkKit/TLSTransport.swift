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

    public init(host: String, port: UInt16, pin: Data, timeout: TimeInterval = 5) throws {
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
        conn.stateUpdateHandler = { state in
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
            if pinMismatch { throw TransportFailure.pinMismatch }
            throw TransportFailure.connect(timedOut ? ETIMEDOUT : ECONNREFUSED)
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
        done.wait()
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
