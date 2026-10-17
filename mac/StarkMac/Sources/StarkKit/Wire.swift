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

    public init(surface: String, home: String, generation: Int) {
        self.surface = surface
        self.home = home
        self.generation = generation
    }
}

public struct HandoffInfo: Codable, Equatable, Sendable {
    public var id: String
    public var from: String
    public var to: String
    public var state: String
    public var fromGen: Int
    public var toGen: Int?

    public init(id: String, from: String, to: String, state: String, fromGen: Int, toGen: Int?) {
        self.id = id
        self.from = from
        self.to = to
        self.state = state
        self.fromGen = fromGen
        self.toGen = toGen
    }
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
    /// The head reply's state (Phase 4.1); nil once it is answered, and from older runtimes.
    public var reply: ReplyState?

    public init(working: Bool = false, callId: String? = nil, state: String? = nil,
                boot: String? = nil, ownerGeneration: Int? = nil, inference: [Call]? = nil,
                work: [Work]? = nil, blocked: [String]? = nil, reply: ReplyState? = nil) {
        self.working = working
        self.callId = callId
        self.state = state
        self.boot = boot
        self.ownerGeneration = ownerGeneration
        self.inference = inference
        self.work = work
        self.blocked = blocked
        self.reply = reply
    }

    /// A responsibility is executing outside inference (a poll, an event handler).
    public var isWorking: Bool { !(work ?? []).isEmpty }

    /// Inference is generating. An older runtime reports only its reply flag.
    public var isThinking: Bool { inference.map { !$0.isEmpty } ?? working }
}

/// What the runtime is doing with the newest user message. The runtime writes no text for
/// Stark when the model cannot answer; a surface shows this line instead.
public struct ReplyState: Codable, Equatable, Sendable {
    public var seq: Int
    public var retry: Int?
    /// queued, thinking, waiting (a retryable refusal), or failed (terminal until retried).
    public var state: String
    /// The runtime's error kind, such as Timeout or ModelUnavailable.
    public var error: String?
    /// Wall clock seconds when a waiting reply tries again; nil when unscheduled.
    public var retryAt: Double?

    public init(seq: Int, retry: Int? = nil, state: String, error: String? = nil,
                retryAt: Double? = nil) {
        self.seq = seq
        self.retry = retry
        self.state = state
        self.error = error
        self.retryAt = retryAt
    }

    public var failed: Bool { state == "failed" }

    /// Still on its way: queued, thinking, or waiting for the model.
    public var pending: Bool { state != "failed" }

    public var caption: String {
        switch state {
        case "queued": return "Stark has your message"
        case "thinking": return "Stark is thinking…"
        case "waiting": return "Waiting for the model" + (reason.map { ": \($0)" } ?? "")
        case "failed": return "No reply" + (reason.map { ": \($0)" } ?? "")
        default: return state
        }
    }

    /// The error kind in words; an unknown kind passes through as the runtime named it.
    public var reason: String? {
        guard let e = error else { return nil }
        switch e {
        case "ModelUnavailable", "ProviderUnavailable", "ModelCrashed", "ModelReplaced":
            return "the model server is unreachable"
        case "Timeout": return "the model took too long"
        case "AttemptsExhausted": return "the model failed three times"
        case "ContextTooLong": return "the message is too long for the model"
        case "AuthFailed": return "the model server refused its credentials"
        case "RateLimited", "Overloaded": return "the model server is busy"
        case "StreamInterrupted": return "the model's answer was cut off"
        default: return e
        }
    }
}

/// The model behind Stark's replies: runtime configuration, never part of his identity.
public struct ModelInfo: Codable, Equatable, Sendable {
    public var provider: String
    public var model: String
    public var locality: String
    /// The deterministic demo model (stark.py run --fake-model).
    public var fake: Bool

    public init(provider: String, model: String, locality: String, fake: Bool) {
        self.provider = provider
        self.model = model
        self.locality = locality
        self.fake = fake
    }
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
    /// A draft an acked jack carried here, awaiting this surface's receipt.
    public var draft: CarriedDraft?
    /// The model behind replies; absent from a runtime before Phase 4.1.
    public var model: ModelInfo?
    /// Optional commands the runtime serves, such as memory; absent before Phase 4.2.
    public var features: [String]?
    /// Saved memory count and whether it reaches the model; absent before Phase 4.2.
    public var memory: MemorySummary?
}

struct ReadyOutResult: Decodable {
    var handoffId: String
    var state: String
    /// Whether the committed jack holds exactly the draft this ready_out sent. Absent from
    /// a runtime older than the receipt, which counts as not carried.
    var draftCarried: Bool?
}

struct AckInResult: Decodable {
    var handoffId: String
    var state: String
    var draft: String?
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

struct RetryResult: Decodable {
    var seq: Int
    var retry: Int
    var replayed: Bool
}

struct SendResult: Decodable {
    var seq: Int
    var replayed: Bool
}
