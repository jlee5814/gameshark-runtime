import Foundation
import Testing
@testable import StarkKit

/// Against the real runtime: `uv run python stark.py run` on a fresh database, driven over
/// the real Unix socket. Enabled by STARK_INTEGRATION=1 (needs uv); CI's swift job sets it.
@Suite(.serialized, .enabled(if: ProcessInfo.processInfo.environment["STARK_INTEGRATION"] == "1"))
final class IntegrationTests {
    let dir: URL
    let prototype: URL
    var daemon: Process?

    init() throws {
        // A short directory: sun_path holds 104 bytes on macOS.
        dir = URL(fileURLWithPath: "/tmp/stark-it-\(getpid())-\(UInt32.random(in: 0...UInt32.max))")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        prototype = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("prototype")
    }

    deinit {
        daemon?.terminate()
        daemon?.waitUntilExit()
        try? FileManager.default.removeItem(at: dir)
    }

    static let fake = ["--fake-model", "--fake-delay", "0.3"]

    func startDaemon(_ extra: [String] = [], model: [String] = IntegrationTests.fake) throws {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = ["uv", "run", "python", "stark.py", "--db", dir.appendingPathComponent("runtime.db").path,
                       "--interval", "1", "run"] + model + extra
        p.currentDirectoryURL = prototype
        var env = ProcessInfo.processInfo.environment
        env["GAMESHARK_TRANSPORT"] = "tests.file_transport:make"
        env["GAMESHARK_FIXTURE"] = dir.appendingPathComponent("github.json").path
        env.removeValue(forKey: "GITHUB_TOKEN")
        p.environment = env
        p.standardOutput = FileHandle(forWritingAtPath: "/dev/null")
        p.standardError = p.standardOutput
        try p.run()
        daemon = p
        #expect(eventually(30) { FileManager.default.fileExists(atPath: sockPath) })
    }

    func stopDaemon() {
        daemon?.terminate()               // SIGTERM: drain and release the lease
        daemon?.waitUntilExit()
        daemon = nil
    }

    /// The daemon's default: gateway/stark.sock beside the database.
    var sockPath: String { dir.appendingPathComponent("gateway/stark.sock").path }

    func client() -> GatewayClient {
        // Told only the socket, the client finds the token beside it, as stark.py writes it.
        var cfg = ClientConfig.fromEnvironment(["STARK_SOCKET": sockPath])
        cfg.backoff = Backoff(base: 0.05, cap: 0.5)
        return GatewayClient(config: cfg)
    }

    func rows(_ c: GatewayClient, role: String) -> [Row] { c.state.rows.filter { $0.role == role } }

    @Test func conversationSurvivesDisconnectRestartAndRejectsObsoleteWrites() throws {
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try Data("{}".utf8).write(to: dir.appendingPathComponent("github.json"))
        try startDaemon()
        let c = client()
        c.start()
        defer { c.stop() }
        #expect(eventually(15) { c.state.connection == .live })
        let uuid = try #require(c.state.identity?.uuid)

        // Send; the inference reply lands while the client stays responsive.
        c.send("hello from swift")
        #expect(eventually(10) { self.rows(c, role: "assistant").count == 1 })

        // Disconnect and reconnect: same rows, no duplicates.
        let before = c.state.rows
        c.reconnectNow()
        #expect(eventually(10) { c.state.connection == .live })
        #expect(c.state.rows == before)

        // An obsolete presence generation writes nothing.
        c.send("from the past", presenceGen: 0)
        #expect(eventually(5) { c.state.outbox.contains { $0.status == .rejected(code: "stale_presence") } })

        // Restart the runtime: Stark comes back with the same identity and conversation.
        stopDaemon()
        #expect(eventually(10) { !c.state.connection.isLive })
        try startDaemon()
        #expect(eventually(30) { c.state.connection == .live })
        #expect(c.state.identity?.uuid == uuid)
        #expect(rows(c, role: "user").map(\.content) == ["hello from swift"])
        c.send("after restart")
        #expect(eventually(10) { self.rows(c, role: "assistant").count == 2 })
        let seqs = c.state.rows.map(\.seq)
        #expect(seqs == Array(0..<seqs.count))       // contiguous, no duplicate
        stopDaemon()
    }

    /// Phase 4.1: a model that never answers leaves no fabricated reply. The runtime says
    /// it is waiting for the model and why, and the Swift client reads it as such.
    @Test func anUnreachableModelShowsWaitingAndNoReply() throws {
        try Data("{}".utf8).write(to: dir.appendingPathComponent("github.json"))
        try startDaemon(model: ["--inference-url", "http://127.0.0.1:9/v1", "--model", "absent",
                                "--model-cooldown", "0.5"])
        let c = client()
        c.start()
        defer { c.stop() }
        #expect(eventually(15) { c.state.connection == .live })
        #expect(c.state.model == ModelInfo(provider: "local", model: "absent", locality: "local",
                                           fake: false))
        c.send("is anyone there?")
        #expect(eventually(30) { c.state.reply?.state == "waiting" })
        #expect(c.state.reply?.caption.hasPrefix("Waiting for the model") == true)
        #expect(rows(c, role: "assistant").isEmpty)
        #expect(!c.state.canRetryReply)                    // waiting retries by itself
        stopDaemon()
    }

    // MARK: paired device over the LAN gateway

    var lanInfo: URL { dir.appendingPathComponent("gateway/stark.sock.lan.json") }

    func lanPort() throws -> UInt16 {
        struct Info: Decodable { var port: UInt16 }
        return try JSONDecoder().decode(Info.self, from: Data(contentsOf: lanInfo)).port
    }

    /// `stark.py pair --json`, exactly as an operator runs it.
    func pairingLink() throws -> PairingLink {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = ["uv", "run", "python", "stark.py", "--db",
                       dir.appendingPathComponent("runtime.db").path, "pair", "--name",
                       "Swift Phone", "--json", "--address", "127.0.0.1"]
        p.currentDirectoryURL = prototype
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle(forWritingAtPath: "/dev/null")
        try p.run()
        p.waitUntilExit()
        struct Printed: Decodable { var link: String }
        let printed = try JSONDecoder().decode(Printed.self,
                                               from: out.fileHandleForReading.readDataToEndOfFile())
        return try #require(PairingLink(printed.link))
    }

    /// A runtime that stops reading (frozen with SIGSTOP, as a peer behind a vanished
    /// interface looks from the phone) fills the socket buffers. A send must then give up
    /// within the transport's timeout instead of holding its caller forever.
    @Test func aSendToARuntimeThatStopsReadingGivesUpWithinItsTimeout() throws {
        try Data("{}".utf8).write(to: dir.appendingPathComponent("github.json"))
        try startDaemon(["--lan", "--lan-port", "0"])
        #expect(eventually(30) { FileManager.default.fileExists(atPath: self.lanInfo.path) })
        struct Info: Decodable { var port: UInt16; var pin: String; var pid: Int32 }
        let info = try JSONDecoder().decode(Info.self, from: Data(contentsOf: lanInfo))
        let pin = try #require(Data(hex: info.pin))
        let t = try TLSTransport(host: "127.0.0.1", port: info.port, pin: pin, timeout: 2)
        #expect(kill(info.pid, SIGSTOP) == 0)               // the runtime, not uv
        defer {
            t.close()
            kill(info.pid, SIGCONT)
        }

        let outcome = Locked<Error?>(nil)
        let done = DispatchSemaphore(value: 0)
        let frame = Data(count: 256 * 1024)
        Thread {
            do {
                for _ in 0..<1024 { try t.send(frame) }     // 256 MiB: far past any buffer
            } catch {
                outcome.value = error
            }
            done.signal()
        }.start()
        #expect(done.wait(timeout: .now() + 15) == .success,
                "send waited past its timeout on a runtime that stopped reading")
        #expect(outcome.value as? TransportFailure == .io(ETIMEDOUT))
    }

    @Test func pairedPhoneJacksInAndOutOverPinnedTLS() throws {
        try Data("{}".utf8).write(to: dir.appendingPathComponent("github.json"))
        try startDaemon(["--lan", "--lan-port", "0"])
        #expect(eventually(30) { FileManager.default.fileExists(atPath: self.lanInfo.path) })
        let link = try pairingLink()
        #expect(link.port == (try lanPort()))

        // A wrong pin fails the TLS handshake before any frame.
        #expect(throws: TransportFailure.pinMismatch) {
            try TLSTransport(host: "127.0.0.1", port: link.port, pin: Data(count: 32))
        }

        let signer = SoftwareSigner()
        let cred = try Pairing.run(link: link, name: "Swift Phone", signer: signer)
        #expect(cred.surface.hasPrefix("pocket-"))
        #expect(throws: Pairing.Failure.refused("auth_failed")) {     // a link works once
            try Pairing.run(link: link, name: "Again", signer: SoftwareSigner())
        }

        let mac = client()
        let phone = GatewayClient(config: ClientConfig(
            device: DeviceEndpoint(credential: cred, signer: signer),
            backoff: Backoff(base: 0.05, cap: 0.5), clientName: "StarkKitTests"))
        mac.start()
        phone.start()
        defer { mac.stop(); phone.stop() }
        #expect(eventually(15) { mac.state.connection == .live && phone.state.connection == .live })
        #expect(phone.state.identity?.uuid == mac.state.identity?.uuid)
        #expect(!phone.state.isActiveSurface)

        // Jack In from the phone: the Mac plays ready_out, the phone acks, both see it.
        phone.jack(.here)
        #expect(eventually(10) {
            phone.state.isActiveSurface && phone.state.handoff == nil && !mac.state.isActiveSurface
        })
        phone.send("from my pocket")
        #expect(eventually(10) { self.rows(mac, role: "assistant").count == 1 })
        #expect(eventually(5) { phone.state.rows == mac.state.rows })

        // Jack Out: Stark goes home; the Mac writes again under the new generation.
        phone.jack(.home)
        #expect(eventually(10) {
            mac.state.isActiveSurface && mac.state.handoff == nil && !phone.state.isActiveSurface
        })
        mac.send("back on the Mac")
        #expect(eventually(10) { self.rows(phone, role: "assistant").count == 2 })
        #expect(eventually(5) { phone.state.rows == mac.state.rows })
        #expect(rows(mac, role: "user").map(\.content) == ["from my pocket", "back on the Mac"])
        stopDaemon()
    }
}
