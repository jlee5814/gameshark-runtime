import Foundation
import Network

/// One way to reach the runtime: an address or name, its port, and which listener it is.
/// The LAN listener answers on the home network; the tailnet listener answers on the Mac's
/// Tailscale address from anywhere the phone's Tailscale reaches. Both present the same
/// pinned key, so a route is only a hint: it decides where to dial, never whom to trust.
public struct Route: Codable, Equatable, Hashable, Sendable {
    public enum Via: String, Codable, Sendable {
        case lan, tailnet
    }

    public var host: String
    public var port: UInt16
    public var via: Via

    public init(host: String, port: UInt16, via: Via) {
        self.host = host
        self.port = port
        self.via = via
    }

    /// Best first for the network the phone is on: the LAN at home, the tailnet away.
    /// Within a kind the pairing link's order holds (numeric address, then name).
    public static func ordered(_ routes: [Route], for path: NetworkPath) -> [Route] {
        let first: Via
        switch path {
        case .wifi, .wired: first = .lan
        case .cellular, .other: first = .tailnet
        case .none: return routes
        }
        return routes.filter { $0.via == first } + routes.filter { $0.via != first }
    }

    /// A Tailscale node address: 100.64.0.0/10 (except tailscaled's 100.100.100.100) or
    /// fd7a:115c:a1e0::/48. The same ranges prototype/gameshark/tailscale.py admits.
    public static func isTailnetAddress(_ text: String) -> Bool {
        if let v4 = IPv4Address(text) {
            let b = [UInt8](v4.rawValue)
            return b[0] == 100 && b[1] & 0xC0 == 64 && b != [100, 100, 100, 100]
        }
        if let v6 = IPv6Address(text) {
            return [UInt8](v6.rawValue).prefix(6).elementsEqual([0xfd, 0x7a, 0x11, 0x5c, 0xa1, 0xe0])
        }
        return false
    }
}

/// The class of network the phone is on, from NWPath. Only a change of class moves a live
/// session, since the old socket rides an interface that may be gone.
public enum NetworkPath: String, Sendable {
    case wifi, wired, cellular, other, none

    public static func classify(satisfied: Bool, wifi: Bool, wired: Bool, cellular: Bool)
        -> NetworkPath
    {
        guard satisfied else { return .none }
        if wifi { return .wifi }
        if wired { return .wired }
        if cellular { return .cellular }
        return .other
    }
}
