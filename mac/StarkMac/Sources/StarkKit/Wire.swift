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

/// A paired device's display name: never a credential.
public struct DeviceName: Codable, Equatable, Sendable {
    public var surface: String
    public var name: String
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

/// What the runtime is executing now: a read only projection, rebuilt from each snapshot.
/// working and callId are the v1 reply flag; the rest arrived with Phase 2.5 and decode as
/// nil from an older runtime.
public struct Activity: Codable, Equatable, Sendable {
    public struct Call: Codable, Equatable, Sendable {
        public var callId: String
        public var conversationId: String
        public var stage: String
    }

    public struct Work: Codable, Equatable, Sendable {
        public var responsibilityId: String
        public var kind: String
        public var count: Int
    }

    public var working: Bool
    public var callId: String?
    public var state: String?
    public var boot: String?
    public var ownerGeneration: Int?
    public var inference: [Call]?
    public var work: [Work]?
    public var blocked: [String]?

    public init(working: Bool = false, callId: String? = nil, state: String? = nil,
                boot: String? = nil, ownerGeneration: Int? = nil, inference: [Call]? = nil,
                work: [Work]? = nil, blocked: [String]? = nil) {
        self.working = working
        self.callId = callId
        self.state = state
        self.boot = boot
        self.ownerGeneration = ownerGeneration
        self.inference = inference
        self.work = work
        self.blocked = blocked
    }

    /// A responsibility is executing outside inference (a poll, an event handler).
    public var isWorking: Bool { !(work ?? []).isEmpty }

    /// Inference is generating. An older runtime reports only its reply flag.
    public var isThinking: Bool { inference.map { !$0.isEmpty } ?? working }
}

struct Head: Decodable {
    var type: String
    var id: Int?
    var kind: String?
}

struct Challenge: Decodable {
    var nonce: String
    var proto: [Int]
    /// The LAN gateway's key pin; absent on the Unix socket.
    var serverFp: String?
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
    /// Paired devices by surface; absent from a runtime before Stark Pocket.
    public var devices: [DeviceName]?
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
