import Foundation
import Observation
import StarkKit

/// Main actor view model. Every runtime fact arrives in ClientState from the gateway; the
/// only local state is UI: the draft, whether the chat is open, and a viewed cursor.
@MainActor @Observable
final class StarkModel {
    private(set) var state: ClientState
    var draft = "" { didSet { draftBox.set(draft) } }
    /// Unsent text Stark brought from another surface while this composer held other text.
    private(set) var draftOffer: CarriedDraft?
    var inputFocused = false { didSet { followRuntime() } }
    var chatOpen = false {
        didSet {
            // SwiftUI reports no focus loss when the field leaves the view with the card.
            if !chatOpen { inputFocused = false }
            markViewed()
        }
    }
    var clickThrough = false
    private(set) var viewedSeq = -1 { didSet { followRuntime() } }

    /// Stark's sprite body, or nil when no atlas loaded (the vector avatar draws instead).
    let stage: SpriteStage?
    /// Device pixels per sheet pixel: 1, 2, or 3. A local preference, never agent state.
    var avatarScale: Int {
        didSet { defaults.set(avatarScale, forKey: Self.scaleKey) }
    }
    var backingScale: CGFloat = 2
    static let scales = [1, 2, 3]
    private static let scaleKey = "avatarScale"

    @ObservationIgnored let client: GatewayClient
    @ObservationIgnored private let draftBox = DraftBox()
    @ObservationIgnored private let defaults = UserDefaults.standard

    init(config: ClientConfig) {
        state = ClientState(surface: config.surface)
        stage = SpriteAtlas.load().map { SpriteStage(atlas: $0) }
        let saved = UserDefaults.standard.integer(forKey: Self.scaleKey)
        avatarScale = Self.scales.contains(saved) ? saved : 1
        let relay = Relay()
        client = GatewayClient(config: config, onChange: { s in relay.forward(s) })
        relay.model = self
        let box = draftBox
        client.setDraftProvider { box.get() }
        followRuntime()
    }

    /// Point size of Stark's body at the current scale and display.
    var avatarSize: CGSize {
        stage?.size(scale: avatarScale, backing: backingScale) ?? CGSize(width: 112, height: 112)
    }

    private func followRuntime() {
        stage?.follow(state, listening: inputFocused, viewedSeq: viewedSeq)
    }

    func start() { client.start() }

    var avatar: AvatarState {
        AvatarState.derive(state, listening: inputFocused, viewedSeq: viewedSeq)
    }

    var canSend: Bool { state.connection.isLive && state.isActiveSurface }

    /// Another surface holds Stark and no jack is open: offer to pull him back here.
    var canJackIn: Bool {
        state.connection.isLive && !state.isActiveSurface && state.handoff == nil
    }

    func jackIn() {
        guard canJackIn else { return }
        client.jack(.here)
    }

    var visibleRows: [Row] { state.rows.filter { $0.role != "system" } }

    /// What the runtime is doing with the newest message; nil once Stark answered.
    var replyLine: String? { state.reply?.caption }

    var canRetryReply: Bool { state.canRetryReply }

    func retryReply() {
        guard canRetryReply, let seq = state.reply?.seq else { return }
        client.retryReply(seq: seq)
    }

    /// Set only for the demo model, so a fake answer is never mistaken for Stark's own.
    var modelBadge: String? { state.model?.fake == true ? "fake model" : nil }

    var attention: [OutgoingMessage] {
        state.outbox.filter { if case .sent = $0.status { false } else { true } }
    }

    func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, canSend else { return }
        client.send(text)
        draft = ""
    }

    func apply(_ s: ClientState) {
        let identityChanged = s.identity?.uuid != state.identity?.uuid
        state = s
        if identityChanged, let uuid = s.identity?.uuid {
            viewedSeq = defaults.object(forKey: viewedKey(uuid)) as? Int ?? -1
        }
        markViewed()
        followDrafts()
        followRuntime()
    }

    /// Restore a carried draft into the composer (never sending it), clear one that left
    /// with Stark, and raise a choice instead of overwriting different text. Quietly: the
    /// chat card stays as it is, so an arriving draft never takes the keyboard from
    /// whatever app the user is typing in; the text waits in the composer.
    private func followDrafts() {
        let steps = DraftContinuity.follow(state, composer: draft, offered: draftOffer)
        if let text = steps.composer { draft = text }
        if let hid = steps.clearSent { client.clearSentDraft(hid) }
        if let hid = steps.confirm { client.confirmDraft(hid) }
        if let offer = steps.offer {
            draftOffer = offer
        } else if steps.withdrawOffer {
            draftOffer = nil
        }
    }

    /// The conflict choice: put the carried text in the composer.
    func useCarriedDraft() {
        guard let offer = draftOffer else { return }
        draft = offer.text
        draftOffer = nil
        client.confirmDraft(offer.handoffId)
    }

    /// The conflict choice: keep what is here; the runtime stops offering the other text.
    func keepCurrentDraft() {
        guard let offer = draftOffer else { return }
        draftOffer = nil
        client.confirmDraft(offer.handoffId)
    }

    private func markViewed() {
        guard chatOpen, let uuid = state.identity?.uuid,
              let newest = state.rows.last(where: { $0.role == "assistant" })?.seq,
              newest > viewedSeq
        else { return }
        viewedSeq = newest
        defaults.set(newest, forKey: viewedKey(uuid))
    }

    private func viewedKey(_ uuid: String) -> String { "viewedSeq.\(uuid)" }
}

/// Hops client callbacks from the gateway queue onto the main actor, in order.
private final class Relay: @unchecked Sendable {
    weak var model: StarkModel?

    func forward(_ s: ClientState) {
        DispatchQueue.main.async {
            MainActor.assumeIsolated { self.model?.apply(s) }
        }
    }
}
