import Darwin
import Foundation

/// A diagnostic line sink: device logs on the phone, nothing on the Mac by default.
public typealias Trace = @Sendable (String) -> Void

/// A byte stream to the runtime. Blocking calls; the client runs reads on its own thread.
public protocol Transport: AnyObject, Sendable {
    /// Write one whole frame.
    func send(_ frame: Data) throws
    /// Block for the next bytes; empty Data means the peer closed.
    func receive() throws -> Data
    func close()
}

public enum TransportFailure: Error, Equatable {
    case pathTooLong
    case connect(Int32)
    case io(Int32)
    case closed
    /// The LAN gateway presented a key other than the pairing pin.
    case pinMismatch
}

/// POSIX AF_UNIX stream socket.
public final class SocketTransport: Transport, @unchecked Sendable {
    private let fd: Int32
    private let writeLock = NSLock()
    private let stateLock = NSLock()
    private var isClosed = false

    public init(path: String) throws {
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let capacity = MemoryLayout.size(ofValue: addr.sun_path)
        let bytes = Array(path.utf8)
        guard bytes.count < capacity else { throw TransportFailure.pathTooLong }
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            raw.copyBytes(from: bytes)
            raw[bytes.count] = 0
        }
        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw TransportFailure.connect(errno) }
        var on: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size))
        let rc = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard rc == 0 else {
            let err = errno
            Darwin.close(fd)
            throw TransportFailure.connect(err)
        }
        self.fd = fd
    }

    public func send(_ frame: Data) throws {
        writeLock.lock()
        defer { writeLock.unlock() }
        try frame.withUnsafeBytes { raw in
            var offset = 0
            while offset < raw.count {
                let n = Darwin.write(fd, raw.baseAddress! + offset, raw.count - offset)
                if n < 0 {
                    if errno == EINTR { continue }
                    throw TransportFailure.io(errno)
                }
                offset += n
            }
        }
    }

    public func receive() throws -> Data {
        var buf = [UInt8](repeating: 0, count: 16 * 1024)
        while true {
            let n = Darwin.read(fd, &buf, buf.count)
            if n > 0 { return Data(buf[0..<n]) }
            if n == 0 { return Data() }
            if errno == EINTR { continue }
            if closed { throw TransportFailure.closed }
            throw TransportFailure.io(errno)
        }
    }

    private var closed: Bool {
        stateLock.lock()
        defer { stateLock.unlock() }
        return isClosed
    }

    public func close() {
        stateLock.lock()
        let already = isClosed
        isClosed = true
        stateLock.unlock()
        guard !already else { return }
        Darwin.shutdown(fd, SHUT_RDWR)
        Darwin.close(fd)
    }

    deinit { close() }
}
