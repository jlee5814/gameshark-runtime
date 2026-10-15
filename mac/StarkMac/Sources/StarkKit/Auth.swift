import CryptoKit
import Foundation

/// Token challenge shared with gameshark/gateway.py. The digest binds the nonce, the surface
/// name, and the protocol version, so an answer is good for one session only.
public enum Auth {
    static let tag = Data("stark-gateway".utf8)

    public static func transcript(nonce: Data, surface: String, proto: Int) -> Data {
        var out = tag
        for part in [nonce, Data(surface.utf8), Data(String(proto).utf8)] {
            out.append(0)
            out.append(part)
        }
        return out
    }

    public static func sign(token: Data, nonce: Data, surface: String, proto: Int) -> String {
        let mac = HMAC<SHA256>.authenticationCode(
            for: transcript(nonce: nonce, surface: surface, proto: proto),
            using: SymmetricKey(data: token))
        return Data(mac).hex
    }

    public enum TokenFailure: Error, Equatable {
        case unreadable(String)
        case tooOpen(String)
        case malformed
    }

    /// Read the runtime's 0600 token file. Refuse one that others can read.
    public static func loadToken(at url: URL) throws -> Data {
        let attrs: [FileAttributeKey: Any]
        do {
            attrs = try FileManager.default.attributesOfItem(atPath: url.path)
        } catch {
            throw TokenFailure.unreadable(url.path)
        }
        if let mode = attrs[.posixPermissions] as? NSNumber, mode.intValue & 0o077 != 0 {
            throw TokenFailure.tooOpen(url.path)
        }
        guard let text = try? String(contentsOf: url, encoding: .utf8),
              let token = Data(hex: text.trimmingCharacters(in: .whitespacesAndNewlines)),
              !token.isEmpty
        else { throw TokenFailure.malformed }
        return token
    }
}

extension Data {
    public var hex: String { map { String(format: "%02x", $0) }.joined() }

    public init?(hex: String) {
        guard hex.count % 2 == 0 else { return nil }
        var bytes = [UInt8]()
        bytes.reserveCapacity(hex.count / 2)
        var i = hex.startIndex
        while i < hex.endIndex {
            let j = hex.index(i, offsetBy: 2)
            guard let b = UInt8(hex[i..<j], radix: 16) else { return nil }
            bytes.append(b)
            i = j
        }
        self.init(bytes)
    }
}
