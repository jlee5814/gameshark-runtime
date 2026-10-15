import Foundation

/// Unsent composer text a jack carried from one surface to another, named by its handoff.
public struct CarriedDraft: Codable, Equatable, Sendable {
    public var handoffId: String
    public var text: String

    public init(handoffId: String, text: String) {
        self.handoffId = handoffId
        self.text = text
    }
}

/// What a surface does with a draft that arrives, and with one that left. Both apps apply
/// the same rules: an arriving draft fills only an empty composer and is never sent; a
/// different text already there is the user's choice; the source clears its composer only
/// when Stark left with exactly that text.
public enum DraftContinuity {
    public enum Arrival: Equatable, Sendable {
        /// Put this text in the composer, then confirm receipt.
        case restore(String)
        /// The composer already holds it; confirm receipt.
        case alreadyHere
        /// The composer holds other text; ask the user, confirm after their choice.
        case conflict
    }

    public static func arrival(of draft: CarriedDraft, composer: String) -> Arrival {
        if composer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return .restore(draft.text)
        }
        return composer == draft.text ? .alreadyHere : .conflict
    }

    public enum Departure: Equatable, Sendable {
        /// The jack is still open; decide later.
        case pending
        /// Stark left with this exact text and it now waits at the target.
        case clearComposer
        /// The jack came back here, or the user kept typing: leave the composer alone.
        case keepComposer
    }

    public static func departure(of sent: CarriedDraft, composer: String,
                                 state: ClientState) -> Departure {
        guard let p = state.presence else { return .pending }
        if state.handoff?.id == sent.handoffId { return .pending }
        if p.surface == state.surface { return .keepComposer }
        return composer == sent.text ? .clearComposer : .keepComposer
    }
}

extension DraftContinuity {
    /// What a surface's model does after a state change, so both apps apply one rule.
    public struct Steps: Equatable, Sendable {
        /// New composer text, when the composer changes.
        public var composer: String?
        /// A carried draft to confirm to the runtime (GatewayClient.confirmDraft).
        public var confirm: String?
        /// A sent draft the model has dealt with (GatewayClient.clearSentDraft).
        public var clearSent: String?
        /// A conflict to show the user; nil keeps the current offer.
        public var offer: CarriedDraft?
        /// The offer shown is no longer valid (Stark left, or it was confirmed).
        public var withdrawOffer = false

        public init() {}
    }

    public static func follow(_ s: ClientState, composer: String,
                              offered: CarriedDraft?) -> Steps {
        var steps = Steps()
        var text = composer
        if let sent = s.sentDraft {
            switch departure(of: sent, composer: composer, state: s) {
            case .pending:
                break
            case .clearComposer:
                steps.composer = ""
                text = ""
                steps.clearSent = sent.handoffId
            case .keepComposer:
                steps.clearSent = sent.handoffId
            }
        }
        guard let incoming = s.incomingDraft else {
            steps.withdrawOffer = offered != nil
            return steps
        }
        if incoming == offered { return steps }       // the user is still choosing
        switch arrival(of: incoming, composer: text) {
        case .restore(let t):
            steps.composer = t
            steps.confirm = incoming.handoffId
        case .alreadyHere:
            steps.confirm = incoming.handoffId
        case .conflict:
            steps.offer = incoming
        }
        return steps
    }
}
