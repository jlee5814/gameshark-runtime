import CryptoKit
import Foundation
import Testing
@testable import StarkKit

/// Shared with prototype/tests/test_devices.py: same transcript bytes, and a Python made
/// signature over them verifies here, so both sides agree on X9.63 keys and DER signatures.
@Suite struct DeviceAuthTests {
    let nonce = Data(repeating: 0xAA, count: 32)
    let fp = Data(0..<32)
    let device = "0123456789abcdef0123456789abcdef"
    let pythonPublicKey = "04515c3d6eb9e396b904d3feca7f54fdcd0cc1e997bf375dca515ad0a6c3b4035f4536be3a50f318fbf9a5475902a221502bef0d57e08c53b2cc0a56f17d9f9354"
    let pythonSignature = "304602210095c76fc03730b3d5b34fd616ce275d761de110dfb1ab57863aad63957ef7cb60022100dc23f9cc1a5d2e1a5794f0988a1d4fd9526fec1ed9687bd7afbf39c7aae5e5a1"

    var transcript: Data { DeviceAuth.transcript(nonce: nonce, deviceID: device, proto: 1, serverFP: fp) }

    @Test func transcriptMatchesThePythonKnownAnswer() {
        #expect(Data(SHA256.hash(data: transcript)).hex
            == "d2528181eaa9e396c8991371ef6acbccc2110fa1b32a72598c2a083590d3c9a4")
    }

    @Test func softwareKeyMatchesPythonEncoding() throws {
        let signer = try SoftwareSigner(rawRepresentation: Data(1...32))
        #expect(signer.publicKey.hex == pythonPublicKey)
        let sig = try P256.Signing.ECDSASignature(derRepresentation: Data(hex: pythonSignature)!)
        let key = try P256.Signing.PublicKey(x963Representation: signer.publicKey)
        #expect(key.isValidSignature(sig, for: transcript))
        // And our own DER signature verifies under the same rules.
        let mine = try P256.Signing.ECDSASignature(derRepresentation: signer.sign(transcript))
        #expect(key.isValidSignature(mine, for: transcript))
        #expect(!key.isValidSignature(mine, for: transcript + Data([0])))
    }

    @Test func transcriptBindsEveryField() {
        let base = transcript
        #expect(DeviceAuth.transcript(nonce: Data(repeating: 0xAB, count: 32), deviceID: device,
                                      proto: 1, serverFP: fp) != base)
        #expect(DeviceAuth.transcript(nonce: nonce, deviceID: device, proto: 2, serverFP: fp) != base)
        #expect(DeviceAuth.transcript(nonce: nonce, deviceID: device, proto: 1,
                                      serverFP: Data(repeating: 1, count: 32)) != base)
    }
}

@Suite struct PairingLinkTests {
    let pin = String(repeating: "ab", count: 32)

    @Test func parsesTheLinkStarkPrints() throws {
        let l = try #require(PairingLink(
            "starkpocket://pair?v=1&h=Jaes-Mac.local&a=192.168.1.20&p=47820&fp=\(pin)&id=0011223344556677&s=c2VjcmV0LXNlY3JldA"))
        #expect(l.host == "Jaes-Mac.local" && l.address == "192.168.1.20" && l.port == 47820)
        #expect(l.pin == Data(repeating: 0xAB, count: 32))
        #expect(l.candidates == ["192.168.1.20", "Jaes-Mac.local"])
        #expect(l.pairingID == "0011223344556677" && l.secret == "c2VjcmV0LXNlY3JldA")
    }

    @Test(arguments: [
        "https://pair?v=1&h=x&p=1&fp=PIN&id=a&s=b",          // another scheme
        "starkpocket://pair?v=2&h=x&p=1&fp=PIN&id=a&s=b",    // another version
        "starkpocket://pair?v=1&h=x&p=0&fp=PIN&id=a&s=b",    // no port
        "starkpocket://pair?v=1&h=x&p=1&fp=abcd&id=a&s=b",   // short pin
        "starkpocket://pair?v=1&h=x&p=1&fp=PIN&id=a",        // no secret
        "starkpocket://pair?v=1&p=1&fp=PIN&id=a&s=b",        // no host
    ])
    func refusesMalformedLinks(_ text: String) {
        #expect(PairingLink(text.replacingOccurrences(of: "PIN", with: pin)) == nil)
    }
}

@Suite struct TailnetRouteTests {
    let pin = String(repeating: "ab", count: 32)
    var base: String { "starkpocket://pair?v=1&h=Jaes-Mac.local&a=192.168.1.20&p=47820&fp=\(pin)&id=0011223344556677&s=c2VjcmV0" }

    @Test func aLinkWithATailnetEndpointYieldsLanThenTailnetRoutes() throws {
        let l = try #require(PairingLink(base + "&t=100.123.57.23&tp=47821&tn=mac.tail7fe60e.ts.net"))
        #expect(l.routes == [Route(host: "192.168.1.20", port: 47820, via: .lan),
                             Route(host: "Jaes-Mac.local", port: 47820, via: .lan),
                             Route(host: "100.123.57.23", port: 47821, via: .tailnet),
                             Route(host: "mac.tail7fe60e.ts.net", port: 47821, via: .tailnet)])
        #expect(l.candidates == ["192.168.1.20", "Jaes-Mac.local"])
    }

    @Test func aLanOnlyLinkHasOnlyLanRoutes() throws {
        let l = try #require(PairingLink(base))
        #expect(l.routes.allSatisfy { $0.via == .lan && $0.port == 47820 })
        #expect(l.routes.count == 2)
    }

    @Test(arguments: [
        "&tp=47821",                                   // a port without an address
        "&t=100.123.57.23",                            // an address without a port
        "&t=100.123.57.23&tp=0",
        "&t=8.8.8.8&tp=47821",                         // not a tailnet address
        "&t=192.168.1.20&tp=47821",
        "&t=100.128.0.1&tp=47821",
        "&t=mac.tail7fe60e.ts.net&tp=47821",           // a name is only a fallback
    ])
    func refusesABadTailnetEndpoint(_ extra: String) {
        #expect(PairingLink(base + extra) == nil)
    }

    @Test(arguments: ["100.64.0.1", "100.127.255.254", "fd7a:115c:a1e0::1"])
    func acceptsTailnetAddresses(_ ip: String) {
        #expect(Route.isTailnetAddress(ip))
    }

    @Test(arguments: ["100.63.255.255", "100.128.0.0", "100.100.100.100", "10.0.0.1",
                      "fd12::1", "::1", "nonsense"])
    func refusesOtherAddresses(_ ip: String) {
        #expect(!Route.isTailnetAddress(ip))
    }

    @Test func aCredentialSavedBeforeTailscaleDecodesToLanRoutes() throws {
        let legacy = """
        {"deviceID":"d","surface":"pocket-a1b2c3","name":"Phone","hosts":["192.168.1.20","Jaes-Mac.local"],
         "port":47820,"pin":"\(Data(repeating: 0xAB, count: 32).base64EncodedString())","agentUUID":"u"}
        """
        let c = try JSONDecoder().decode(DeviceCredential.self, from: Data(legacy.utf8))
        #expect(c.routes == [Route(host: "192.168.1.20", port: 47820, via: .lan),
                             Route(host: "Jaes-Mac.local", port: 47820, via: .lan)])
    }

    @Test func aCredentialWithRoutesRoundTrips() throws {
        let l = try #require(PairingLink(base + "&t=100.123.57.23&tp=47821"))
        let c = DeviceCredential(deviceID: "d", surface: "pocket-a1b2c3", name: "Phone",
                                 hosts: l.candidates, port: l.port, pin: l.pin, agentUUID: "u",
                                 routes: l.routes)
        let back = try JSONDecoder().decode(DeviceCredential.self, from: JSONEncoder().encode(c))
        #expect(back == c && back.routes.last?.via == .tailnet)
    }
}
