import Foundation

/// Wire framing shared with gameshark/gateway.py: a 4 byte big endian length, then UTF8 JSON.
public enum Frame {
    public static let maxBytes = 64 * 1024

    public enum Failure: Error, Equatable {
        case tooLarge(Int)
        case empty
    }

    public static func encode(_ body: Data) throws -> Data {
        guard body.count <= maxBytes else { throw Failure.tooLarge(body.count) }
        var n = UInt32(body.count).bigEndian
        var out = Data(bytes: &n, count: 4)
        out.append(body)
        return out
    }

    public static func encode(json object: [String: Any]) throws -> Data {
        try encode(JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]))
    }
}

/// Incremental decoder: feed bytes as they arrive, take whole frame bodies out.
public struct FrameReader {
    private var buffer = Data()

    public init() {}

    public mutating func feed(_ bytes: Data) {
        buffer.append(bytes)
    }

    /// The next complete frame body, nil when more bytes are needed.
    public mutating func next() throws -> Data? {
        guard buffer.count >= 4 else { return nil }
        let start = buffer.startIndex
        let n = buffer[start..<start + 4].reduce(0) { ($0 << 8) | Int($1) }
        guard n > 0 else { throw Frame.Failure.empty }
        guard n <= Frame.maxBytes else { throw Frame.Failure.tooLarge(n) }
        guard buffer.count >= 4 + n else { return nil }
        let body = buffer.subdata(in: start + 4..<start + 4 + n)
        buffer.removeSubrange(start..<start + 4 + n)
        return body
    }
}
