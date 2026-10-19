import Foundation

/// Push to talk as a pure state machine over the states GatewayClient publishes.
///
/// Only the surface holding Stark's presence may listen: identity known, live, present, and
/// no jack open. Anything else, or any change of who holds him, discards the voice draft.
/// Permission prompts never start the microphone; a fresh press does. Recognition results
/// carry their session, so a late callback from a cancelled capture changes nothing.
/// The machine never sends: the voice draft leaves only through an explicit Send or Insert.
public struct CaptureMachine: Sendable {
    public enum Phase: Equatable, Sendable {
        case idle
        /// A permission prompt is up; the press that raised it is spent.
        case requesting
        case capturing(Int)
        /// Released; waiting for the final transcript.
        case finishing(Int)
        /// The transcript is in the voice draft for the user to edit, send, or discard.
        case reviewing
    }

    /// Why a voice draft was thrown away.
    public enum Reason: String, Sendable {
        case user, escape, background, sleep, chatClosed = "chat_closed", jack, unpaired
        /// Presence, connection, or identity changed under the capture.
        case presence
        /// The microphone or route was taken away.
        case interrupted
        case failed
        /// Released without any words recognized.
        case empty
    }

    public enum Rejection: Equatable, Sendable {
        case notPresent, busy
        case availability(InputAvailability)
    }

    public enum Event: Sendable {
        case state(ClientState)
        case press(InputAvailability)
        case permission(InputAvailability)
        case release
        case input(Int, InputEvent)
        case finishTimeout(Int)
        case edit(String)
        case cancel(Reason)
        /// The reviewed text was submitted, or moved into the composer.
        case sent
        case inserted
    }

    public enum Effect: Equatable, Sendable {
        /// Silence Stark and keep him silent while the microphone is open.
        case holdSpeech
        case releaseSpeech
        case requestPermission
        case startRecognizer(Int)
        case finishRecognizer(Int)
        case cancelRecognizer
    }

    /// What a step did, for diagnostics and the user facing notice; never text.
    public enum Decision: Equatable, Sendable {
        case rejected(Rejection)
        case askedPermission
        case permission(InputAvailability)
        case started(Int)
        case captured(Int, chars: Int)
        case timedOut(Int, chars: Int)
        case discarded(Reason, chars: Int)
        case sent(chars: Int)
        case inserted(chars: Int)
    }

    /// Who holds Stark when a capture may run; any change discards the voice draft.
    public struct Key: Equatable, Sendable {
        var uuid: String
        var surface: String
        var generation: Int
    }

    public private(set) var phase: Phase = .idle
    /// The voice draft: local to this device, never in DraftBox, never persisted.
    public private(set) var draft = ""
    public private(set) var key: Key?
    public private(set) var session = 0
    public private(set) var lastDecision: Decision?
    /// Text kept from an earlier take; a new press appends to it.
    private var prefix = ""
    private var lastRevision = Int.min

    public init() {}

    public var isPresent: Bool { key != nil }

    public var isListening: Bool {
        switch phase {
        case .capturing, .finishing: true
        default: false
        }
    }

    public mutating func step(_ e: Event) -> [Effect] {
        lastDecision = nil
        switch e {
        case .state(let s):
            guard s.revision >= lastRevision else { return [] }
            lastRevision = s.revision
            let now = Self.holder(s)
            guard now != key else { return [] }
            key = now
            return discard(.presence)
        case .press(let availability):
            return press(availability)
        case .permission(let result):
            guard phase == .requesting else { return [] }
            phase = draft.isEmpty ? .idle : .reviewing
            lastDecision = .permission(result)
            return []
        case .release:
            guard case .capturing(let n) = phase else { return [] }
            phase = .finishing(n)
            return [.finishRecognizer(n)]
        case .input(let n, let event):
            return input(n, event)
        case .finishTimeout(let n):
            guard phase == .finishing(n) else { return [] }
            return end(n, timedOut: true)
        case .edit(let text):
            guard phase == .reviewing else { return [] }
            draft = text
            return []
        case .cancel(let why):
            return discard(why)
        case .sent:
            guard phase == .reviewing else { return [] }
            lastDecision = .sent(chars: draft.count)
            clear()
            return []
        case .inserted:
            guard phase == .reviewing else { return [] }
            lastDecision = .inserted(chars: draft.count)
            clear()
            return []
        }
    }

    static func holder(_ s: ClientState) -> Key? {
        guard let uuid = s.identity?.uuid, s.connection.isLive, s.isActiveSurface,
              let p = s.presence, s.handoff == nil else { return nil }
        return Key(uuid: uuid, surface: s.surface, generation: p.generation)
    }

    private mutating func press(_ availability: InputAvailability) -> [Effect] {
        guard key != nil else { return reject(.notPresent) }
        guard phase == .idle || phase == .reviewing else { return reject(.busy) }
        switch availability {
        case .ready:
            session += 1
            prefix = draft.isEmpty ? "" : draft + " "
            phase = .capturing(session)
            lastDecision = .started(session)
            return [.holdSpeech, .startRecognizer(session)]
        case .needsPermission:
            phase = .requesting
            lastDecision = .askedPermission
            return [.requestPermission]
        case .denied, .restricted, .unavailable:
            return reject(.availability(availability))
        }
    }

    private mutating func input(_ n: Int, _ event: InputEvent) -> [Effect] {
        guard phase == .capturing(n) || phase == .finishing(n) else { return [] }
        switch event {
        case .partial(let text):
            draft = prefix + text
            return []
        case .final(let text):
            draft = prefix + text
            return end(n, timedOut: false)
        case .failed:
            return discard(.failed)
        case .interrupted:
            return discard(.interrupted)
        }
    }

    /// The capture is over; whatever was heard waits for review.
    private mutating func end(_ n: Int, timedOut: Bool) -> [Effect] {
        let chars = draft.count
        if draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            clear()
            lastDecision = .discarded(.empty, chars: 0)
        } else {
            phase = .reviewing
            prefix = ""
            lastDecision = timedOut ? .timedOut(n, chars: chars) : .captured(n, chars: chars)
        }
        return [.cancelRecognizer, .releaseSpeech]
    }

    private mutating func discard(_ why: Reason) -> [Effect] {
        let listening = isListening
        guard phase != .idle || !draft.isEmpty else { return [] }
        lastDecision = .discarded(why, chars: draft.count)
        clear()
        return listening ? [.cancelRecognizer, .releaseSpeech] : []
    }

    private mutating func reject(_ why: Rejection) -> [Effect] {
        lastDecision = .rejected(why)
        return []
    }

    private mutating func clear() {
        phase = .idle
        draft = ""
        prefix = ""
    }
}
