import Foundation
import Network
import os
import StarkKit

/// Connection diagnostics in the unified log, subsystem dev.gameshark.starkpocket, category
/// net. Hosts, ports, route kinds, states, and error codes only: never the pin, the pairing
/// secret, or key material. Read with `log collect --device-udid` and `log show`.
enum NetLog {
    static let logger = Logger(subsystem: "dev.gameshark.starkpocket", category: "net")

    static let trace: Trace = { line in logger.notice("\(line, privacy: .public)") }

    /// The commit this app was built from, stamped by the build; "unstamped" otherwise.
    static let build = Bundle.main.object(forInfoDictionaryKey: "StarkBuildSHA") as? String
        ?? "unstamped"

    static func routes(_ routes: [Route]) -> [String] {
        routes.map { "\($0.via.rawValue) \($0.host):\($0.port)" }
    }

    /// Every fact NWPathMonitor reports that bears on classification, with the class chosen.
    static func describe(_ path: NWPath, as chosen: NetworkPath, initial: Bool) -> String {
        let kinds: [(String, NWInterface.InterfaceType)] = [
            ("wifi", .wifi), ("cellular", .cellular), ("wired", .wiredEthernet),
            ("other", .other), ("loopback", .loopback)]
        let uses = kinds.filter { path.usesInterfaceType($0.1) }.map(\.0).joined(separator: "+")
        let available = path.availableInterfaces.map { "\($0.name)/\($0.type)" }
            .joined(separator: ",")
        var reason = ""
        if path.status != .satisfied { reason = " unsatisfied=\(path.unsatisfiedReason)" }
        return "nwpath\(initial ? " initial" : "") status=\(path.status)\(reason) "
            + "uses=\(uses.isEmpty ? "none" : uses) available=[\(available)] "
            + "expensive=\(path.isExpensive) constrained=\(path.isConstrained) "
            + "class=\(chosen.rawValue)"
    }
}

/// Whether the monitor has delivered a path yet; read and written on its queue only.
final class FirstPath: @unchecked Sendable {
    var value = false
}
