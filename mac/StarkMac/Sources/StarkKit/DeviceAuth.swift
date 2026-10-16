import CryptoKit
import Foundation

/// A paired device's key. The runtime stores only its public half (gameshark/devices.py).
public protocol DeviceSigner: Sendable {
    /// X9.63 uncompressed P256 point, 65 bytes.
    var publicKey: Data { get }
    /// ECDSA P256 SHA256 over message, DER encoded.
    func sign(_ message: Data) throws -> Data
}

/// A software key: tests, the simulator, and Macs without a Secure Enclave.
public struct SoftwareSigner: DeviceSigner {
    let key: P256.Signing.PrivateKey

    public init(key: P256.Signing.PrivateKey = P256.Signing.PrivateKey()) {
        self.key = key
    }

    public init(rawRepresentation: Data) throws {
        key = try P256.Signing.PrivateKey(rawRepresentation: rawRepresentation)
    }

    public var rawRepresentation: Data { key.rawRepresentation }
    public var publicKey: Data { key.publicKey.x963Representation }

    public func sign(_ message: Data) throws -> Data {
        try key.signature(for: message).derRepresentation
    }
}

/// A Secure Enclave key: the private half never leaves the chip. dataRepresentation is an
/// encrypted handle only this device's enclave can use.
public struct EnclaveSigner: DeviceSigner {
    let key: SecureEnclave.P256.Signing.PrivateKey

    public static var isAvailable: Bool { SecureEnclave.isAvailable }

    public init() throws {
        key = try SecureEnclave.P256.Signing.PrivateKey()
    }

    public init(dataRepresentation: Data) throws {
        key = try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: dataRepresentation)
    }

    public var dataRepresentation: Data { key.dataRepresentation }
    public var publicKey: Data { key.publicKey.x963Representation }

    public func sign(_ message: Data) throws -> Data {
        try key.signature(for: message).derRepresentation
    }
}

/// The device proof shared with gameshark/devices.py:
/// "stark-device" 0 nonce 0 device_id 0 proto 0 server_key_sha256.
public enum DeviceAuth {
    static let tag = Data("stark-device".utf8)

    public static func transcript(nonce: Data, deviceID: String, proto: Int, serverFP: Data) -> Data {
        var out = tag
        for part in [nonce, Data(deviceID.utf8), Data(String(proto).utf8), serverFP] {
            out.append(0)
            out.append(part)
        }
        return out
    }

    /// SHA256 of an X9.63 public key: the pin stark.py prints in the pairing link.
    public static func pin(ofPublicKey raw: Data) -> Data {
        Data(SHA256.hash(data: raw))
    }
}

/// `starkpocket://pair?v=1&h=<host>&a=<address>&p=<port>[&t=<tailnet ip>&tp=<port>&tn=<name>]
/// &fp=<pin>&id=<pairing>&s=<secret>`, printed by `stark.py pair`. Single use and short
/// lived; the secret is the only credential. The t keys appear when the runtime also serves
/// its Tailscale address (`run --lan --tailscale`); older links simply lack them.
public struct PairingLink: Equatable, Sendable {
    public var host: String
    public var address: String?
    public var port: UInt16
    public var tailnetAddress: String?
    public var tailnetPort: UInt16?
    public var tailnetName: String?
    public var pin: Data
    public var pairingID: String
    public var secret: String

    public init?(_ text: String) {
        guard let c = URLComponents(string: text.trimmingCharacters(in: .whitespacesAndNewlines)),
              c.scheme == "starkpocket", c.host == "pair" || c.path == "pair" || c.path == "//pair"
        else { return nil }
        var q: [String: String] = [:]
        for item in c.queryItems ?? [] { q[item.name] = item.value }
        guard q["v"] == "1", let host = q["h"], !host.isEmpty,
              let port = q["p"].flatMap(UInt16.init), port > 0,
              let pin = q["fp"].flatMap({ Data(hex: $0) }), pin.count == 32,
              let id = q["id"], !id.isEmpty, let secret = q["s"], !secret.isEmpty
        else { return nil }
        // A tailnet endpoint comes whole (a Tailscale address and its port) or not at all.
        let t = q["t"].flatMap { $0.isEmpty ? nil : $0 }
        let tp = q["tp"].flatMap { $0.isEmpty ? nil : $0 }
        if t != nil || tp != nil {
            guard let t, Route.isTailnetAddress(t), let tp = tp.flatMap(UInt16.init), tp > 0
            else { return nil }
            self.tailnetAddress = t
            self.tailnetPort = tp
            self.tailnetName = q["tn"].flatMap { $0.isEmpty ? nil : $0 }
        }
        self.host = host
        self.address = q["a"].flatMap { $0.isEmpty ? nil : $0 }
        self.port = port
        self.pin = pin
        self.pairingID = id
        self.secret = secret
    }

    /// LAN hosts, best first: the numeric address, then the Bonjour host name.
    public var candidates: [String] { [address, host].compactMap { $0 } }

    /// Every way in: the LAN hosts on the LAN port, then the tailnet address and its
    /// MagicDNS name on the tailnet port.
    public var routes: [Route] {
        var out = candidates.map { Route(host: $0, port: port, via: .lan) }
        if let tailnetPort {
            out += [tailnetAddress, tailnetName].compactMap { $0 }
                .map { Route(host: $0, port: tailnetPort, via: .tailnet) }
        }
        return out
    }
}

/// What a phone keeps after pairing (in its Keychain). The pin decides trust; addresses are
/// only hints, so a changed DHCP lease costs a retry, never trust.
public struct DeviceCredential: Codable, Equatable, Sendable {
    public var deviceID: String
    public var surface: String
    public var name: String
    public var hosts: [String]
    public var port: UInt16
    public var pin: Data
    public var agentUUID: String
    /// Where to dial. A pairing stored before Tailscale support has none saved, and gets
    /// its LAN hosts on its LAN port.
    public var routes: [Route]

    public init(deviceID: String, surface: String, name: String, hosts: [String], port: UInt16,
                pin: Data, agentUUID: String, routes: [Route]? = nil) {
        self.deviceID = deviceID
        self.surface = surface
        self.name = name
        self.hosts = hosts
        self.port = port
        self.pin = pin
        self.agentUUID = agentUUID
        self.routes = routes ?? hosts.map { Route(host: $0, port: port, via: .lan) }
    }

    private enum CodingKeys: String, CodingKey {
        case deviceID, surface, name, hosts, port, pin, agentUUID, routes
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(deviceID: try c.decode(String.self, forKey: .deviceID),
                  surface: try c.decode(String.self, forKey: .surface),
                  name: try c.decode(String.self, forKey: .name),
                  hosts: try c.decode([String].self, forKey: .hosts),
                  port: try c.decode(UInt16.self, forKey: .port),
                  pin: try c.decode(Data.self, forKey: .pin),
                  agentUUID: try c.decode(String.self, forKey: .agentUUID),
                  routes: try c.decodeIfPresent([Route].self, forKey: .routes))
    }
}
