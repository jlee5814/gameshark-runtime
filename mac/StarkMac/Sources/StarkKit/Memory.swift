import Foundation

/// A fact the user explicitly asked Stark to keep. The runtime owns it; a surface only
/// renders the list and sends typed commands.
public struct MemoryItem: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var content: String
    public var revision: Int
    public var scope: String
    public var state: String
    public var sourceSurface: String
    public var sourceDeviceId: String?
    public var created: Double
    public var updated: Double

    public init(id: String, content: String, revision: Int, scope: String = "global",
                state: String = "active", sourceSurface: String, sourceDeviceId: String? = nil,
                created: Double = 0, updated: Double = 0) {
        self.id = id
        self.content = content
        self.revision = revision
        self.scope = scope
        self.state = state
        self.sourceSurface = sourceSurface
        self.sourceDeviceId = sourceDeviceId
        self.created = created
        self.updated = updated
    }
}

/// A memory write waiting for the runtime's answer. Its op id is chosen once, so a resend
/// after a reconnect lands at most once.
public struct MemoryOp: Equatable, Sendable, Identifiable {
    public enum Kind: Equatable, Sendable {
        case remember(text: String)
        case correct(memoryId: String, revision: Int, text: String)
        case forget(memoryId: String)
    }

    public enum Status: Equatable, Sendable {
        case pending
        case rejected(code: String)
    }

    public var id: String
    public var kind: Kind
    public var status: Status

    /// The text a pending or refused write would leave, for the list to show in place.
    public var text: String? {
        switch kind {
        case .remember(let t), .correct(_, _, let t): t
        case .forget: nil
        }
    }

    public var memoryId: String? {
        switch kind {
        case .remember: nil
        case .correct(let id, _, _), .forget(let id): id
        }
    }
}

/// What a composer line asks for. Only an explicit command saves memory; ordinary chat is
/// never scanned for facts.
public enum MemoryCommand: Equatable, Sendable {
    case remember(String)
    case show

    static let rememberPrefix = "/remember"
    static let showCommands: Set<String> = ["/memories", "/memory"]

    /// nil for an ordinary message, which goes to the conversation unchanged.
    public static func parse(_ line: String) -> MemoryCommand? {
        let text = line.trimmingCharacters(in: .whitespacesAndNewlines)
        let lower = text.lowercased()
        if showCommands.contains(lower) { return .show }
        guard lower.hasPrefix(rememberPrefix) else { return nil }
        let rest = text.dropFirst(rememberPrefix.count)
        guard let first = rest.first else { return .show }     // bare /remember: open the list
        guard first.isWhitespace else { return nil }             // /rememberable is a message
        let fact = rest.trimmingCharacters(in: .whitespacesAndNewlines)
        return fact.isEmpty ? .show : .remember(fact)
    }
}

public enum MemoryText {
    /// What a surface says after a forget. Honest about history: saved memory is gone, but
    /// chat rows that quote the fact stay in the conversation and can still reach the model
    /// while they are recent.
    public static func forgotten(historyMentions n: Int) -> String {
        guard n > 0 else { return "Removed from saved memory." }
        let rows = n == 1 ? "1 earlier chat message still contains" :
            "\(n) earlier chat messages still contain"
        return "Removed from saved memory. \(rows) this text and can reach Stark while recent."
    }

    public static func refused(_ code: String) -> String {
        switch code {
        case "stale_presence": "Not saved: Stark is on another surface. Jack in to change memory."
        case "conflict": "Not saved: this memory changed elsewhere. Review it and try again."
        case "forbidden": "Not saved: memory writes are turned off on the Mac."
        case "memory_limit": "Not saved: Stark already keeps 256 memories. Forget one first."
        case "duplicate": "Not saved: another memory already says exactly this."
        case "invalid": "Not saved: the text is empty or too long (1 KB at most)."
        default: "Not saved: \(code)"
        }
    }
}

struct MemoriesPayload: Decodable {
    var memories: [MemoryItem]
    var inject: Bool
}

struct MemoryWriteResult: Decodable {
    var memory: MemoryItem
    var replayed: Bool
}

struct ForgetResult: Decodable {
    var memoryId: String
    var state: String
    var replayed: Bool
    var historyMentions: Int
}

public struct MemorySummary: Decodable, Equatable, Sendable {
    public var count: Int
    public var inject: Bool
}
