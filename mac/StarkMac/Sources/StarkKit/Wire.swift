import Foundation

/// Protocol v1 frames. Field names follow the runtime's snake case through the decoder.
public enum Wire {
    public static let protocolVersion = 1
    public static let conversation = "stark:main"

    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    public static func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        try decoder.decode(type, from: data)
    }
}

public struct Identity: Codable, Equatable, Sendable {
    public var agentId: String
    public var uuid: String
}

public struct PresenceInfo: Codable, Equatable, Sendable {
    public var surface: String
    public var home: String
    public var generation: Int
}

public struct HandoffInfo: Codable, Equatable, Sendable {
    public var id: String
    public var from: String
    public var to: String
    public var state: String
    public var fromGen: Int
    public var toGen: Int?
}

public struct Responsibility: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var kind: String
    public var state: String
    public var checkpoint: String?
}

public struct Row: Codable, Equatable, Sendable, Identifiable {
    public var seq: Int
    public var role: String
    public var content: String
    public var id: Int { seq }
}

public struct Activity: Codable, Equatable, Sendable {
    public var working: Bool
    public var callId: String?
}

struct Head: Decodable {
    var type: String
    var id: Int?
    var kind: String?
}

struct Challenge: Decodable {
    var nonce: String
    var proto: [Int]
}

struct Welcome: Decodable {
    var proto: Int
    var session: String
    var surface: String
    var identity: Identity
}

struct ErrorFrame: Decodable {
    var code: String
    var message: String?
}

struct Result<T: Decodable>: Decodable {
    var data: T
}

struct Event<T: Decodable>: Decodable {
    var rev: Int
    var data: T
}

public struct Snapshot: Decodable, Equatable, Sendable {
    public struct Conversation: Decodable, Equatable, Sendable {
        public var id: String
        public var headSeq: Int
    }

    public var server: String
    public var identity: Identity
    public var ownerGeneration: Int?
    public var you: String
    public var presence: PresenceInfo
    public var handoff: HandoffInfo?
    public var responsibilities: [Responsibility]
    public var conversation: Conversation
    public var activity: Activity
}

struct PresenceEvent: Decodable {
    var presence: PresenceInfo
    var handoff: HandoffInfo?
}

struct ResponsibilitiesEvent: Decodable {
    var responsibilities: [Responsibility]
}

struct RowsPayload: Decodable {
    var conversationId: String
    var rows: [Row]
    var more: Bool?
}

struct SendResult: Decodable {
    var seq: Int
    var replayed: Bool
}
