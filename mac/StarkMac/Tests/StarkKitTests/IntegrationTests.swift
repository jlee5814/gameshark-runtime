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

    func startDaemon() throws {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = ["uv", "run", "python", "stark.py", "--db", dir.appendingPathComponent("runtime.db").path,
                       "--interval", "1", "run", "--fake-delay", "0.3"]
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
        let sock = dir.appendingPathComponent("stark.sock").path
        #expect(eventually(30) { FileManager.default.fileExists(atPath: sock) })
    }

    func stopDaemon() {
        daemon?.terminate()               // SIGTERM: drain and release the lease
        daemon?.waitUntilExit()
        daemon = nil
    }

    func client() -> GatewayClient {
        GatewayClient(config: ClientConfig(
            socketPath: dir.appendingPathComponent("stark.sock").path,
            tokenURL: dir.appendingPathComponent("gateway.token"),
            backoff: Backoff(base: 0.05, cap: 0.5)))
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
}
