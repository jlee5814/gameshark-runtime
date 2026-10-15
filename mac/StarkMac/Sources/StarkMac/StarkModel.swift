import Foundation
import Observation
import StarkKit

/// Main actor view model. Every runtime fact arrives in ClientState from the gateway; the
/// only local state is UI: the draft, whether the chat is open, and a viewed cursor.
@MainActor @Observable
final class StarkModel {
    private(set) var state: ClientState
    var draft = "" { didSet { draftBox.set(draft) } }
    var inputFocused = false
    var chatOpen = false { didSet { markViewed() } }
    var clickThrough = false
    private(set) var viewedSeq = -1

    @ObservationIgnored let client: GatewayClient
    @ObservationIgnored private let draftBox = DraftBox()
    @ObservationIgnored private let defaults = UserDefaults.standard

    init(config: ClientConfig) {
        state = ClientState(surface: config.surface)
        let relay = Relay()
        client = GatewayClient(config: config, onChange: { s in relay.forward(s) })
        relay.model = self
        let box = draftBox
        client.setDraftProvider { box.get() }
    }

    func start() { client.start() }

    var avatar: AvatarState {
        AvatarState.derive(state, listening: inputFocused, viewedSeq: viewedSeq)
    }

    var canSend: Bool { state.connection.isLive && state.isActiveSurface }

    var visibleRows: [Row] { state.rows.filter { $0.role != "system" } }

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

/// The draft, readable from the gateway queue when a jack asks for it.
private final class DraftBox: @unchecked Sendable {
    private let lock = NSLock()
    private var value = ""

    func set(_ v: String) { lock.withLock { value = v } }
    func get() -> String? { lock.withLock { value.isEmpty ? nil : value } }
}
