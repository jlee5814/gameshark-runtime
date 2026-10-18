import Foundation

/// When Stark speaks, as a pure state machine over the states GatewayClient publishes.
///
/// A surface speaks a new assistant row only while armed: identity known, live, holding
/// presence, and no jack open. Arming raises the handled watermark to the newest row, so
/// history, sync catch up, reconnect replay, and relaunch are never news. The runtime
/// pushes presence before messages on every connection, so a surface sees a jack open or
/// presence leave before any row committed after it: two surfaces never speak one reply.
/// The watermark persists before the engine speaks, so a killed utterance never replays.
public struct SpeechMachine: Sendable {
    public enum Disarm: Equatable, Sendable {
        case noIdentity, offline, ghost, handoff, suspended
    }

    /// Presence as this surface held it when it armed; any change re-arms.
    public struct ArmKey: Equatable, Sendable {
        var uuid: String
        var surface: String
        var generation: Int
    }

    public enum Gate: Equatable, Sendable {
        case disarmed(Disarm)
        case armed(ArmKey)
    }

    public enum Event: Sendable {
        case state(ClientState)
        case setMuted(Bool)
        /// The surface left the screen (iOS background); rows arriving meanwhile are history.
        case setSuspended(Bool)
        case replay(Int)
        /// The user asked for silence: Stop, the avatar, or a new message.
        case stop
        /// The system took the audio away.
        case interrupted
        case finished(Utterance.ID)
    }

    /// Why a reply that could have been spoken was not; read only by diagnostics.
    public enum Decision: Equatable, Sendable {
        case skippedMuted(seq: Int)
        case replayRejected(seq: Int, SpeechDiagnostic.Rejection)
    }

    public enum Effect: Equatable, Sendable {
        case persist(key: String, seq: Int)
        case stopEngine
        case speak(Utterance.ID, String)
    }

    public let conversation: String
    public private(set) var gate: Gate = .disarmed(.noIdentity)
    public private(set) var speaking: Utterance.ID?
    public private(set) var muted: Bool
    public private(set) var suspended = false
    /// The ledger key of the identity last seen, and its watermark.
    public private(set) var ledgerKey: String?
    public private(set) var handled = -1
    /// Set by the last step when it declined to speak; effects never depend on it.
    public private(set) var lastDecision: Decision?
    private var lastRevision = Int.min
    private var assistantRows: [Int: String] = [:]
    private var token = 0

    public init(muted: Bool = false, conversation: String = Wire.conversation) {
        self.muted = muted
        self.conversation = conversation
    }

    public var isArmed: Bool { if case .armed = gate { true } else { false } }

    /// Apply one event; `load` reads a persisted watermark when the identity changes.
    public mutating func step(_ e: Event, load: (String) -> Int?) -> [Effect] {
        lastDecision = nil
        switch e {
        case .state(let s):
            return observe(s, load: load)
        case .setMuted(let m):
            muted = m
            return m ? silence() : []
        case .setSuspended(let hidden):
            suspended = hidden
            return hidden ? disarm(.suspended) : []
        case .replay(let seq):
            guard isArmed, let text = assistantRows[seq] else {
                if case .disarmed(let why) = gate {
                    lastDecision = .replayRejected(seq: seq, .disarmed(why))
                } else {
                    lastDecision = .replayRejected(seq: seq, .unknownRow)
                }
                return []
            }
            return silence() + say(seq, text)
        case .stop, .interrupted:
            return silence()
        case .finished(let id):
            if speaking == id { speaking = nil }
            return []
        }
    }

    private mutating func observe(_ s: ClientState, load: (String) -> Int?) -> [Effect] {
        // A state published before one already seen arrived late: it is not news.
        guard s.revision >= lastRevision else { return [] }
        lastRevision = s.revision
        assistantRows = Dictionary(s.rows.filter { $0.role == "assistant" }
            .map { ($0.seq, $0.content) }, uniquingKeysWith: { a, _ in a })
        var fx: [Effect] = []
        guard let uuid = s.identity?.uuid else { return disarm(.noIdentity) }
        let key = SpeechLedger.key(agent: uuid, conversation: conversation, surface: s.surface)
        if key != ledgerKey {
            fx += silence()
            ledgerKey = key
            handled = load(key) ?? -1
        }
        guard s.connection.isLive else { return fx + disarm(.offline) }
        guard s.isActiveSurface, let p = s.presence else { return fx + disarm(.ghost) }
        guard s.handoff == nil else { return fx + disarm(.handoff) }
        guard !suspended else { return fx + disarm(.suspended) }
        let head = s.rows.map(\.seq).max() ?? -1
        let arm = ArmKey(uuid: uuid, surface: s.surface, generation: p.generation)
        if gate != .armed(arm) {
            // Everything already here arrived before this surface could speak.
            gate = .armed(arm)
            return fx + silence() + raise(to: head)
        }
        let fresh = s.rows.filter { $0.seq > handled }.sorted { $0.seq < $1.seq }
        guard let newest = fresh.last else { return fx }
        fx += raise(to: newest.seq)
        // A newer user message supersedes whatever Stark was saying, and any reply before it.
        if fresh.contains(where: { $0.role == "user" }) { fx += silence() }
        if !muted, newest.role == "assistant" {
            fx += silence() + say(newest.seq, newest.content)
        } else if newest.role == "assistant" {
            lastDecision = .skippedMuted(seq: newest.seq)
        }
        return fx
    }

    private mutating func disarm(_ why: Disarm) -> [Effect] {
        gate = .disarmed(why)
        return silence()
    }

    private mutating func silence() -> [Effect] {
        guard speaking != nil else { return [] }
        speaking = nil
        return [.stopEngine]
    }

    private mutating func raise(to seq: Int) -> [Effect] {
        guard seq > handled, let key = ledgerKey else { return [] }
        handled = seq
        return [.persist(key: key, seq: seq)]
    }

    private mutating func say(_ seq: Int, _ text: String) -> [Effect] {
        token += 1
        let id = Utterance.ID(conversation: conversation, seq: seq, token: token)
        speaking = id
        return [.speak(id, text)]
    }
}
