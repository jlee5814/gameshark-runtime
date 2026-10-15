import StarkKit

/// What the header says and which jack control it offers, derived only from ClientState.
/// Pure, so it tests without a runtime.
struct PresenceSummary: Equatable {
    enum Control: Equatable {
        case jackIn, jackOut, none
    }

    var label: String
    var control: Control
    var canSend: Bool

    static func of(_ s: ClientState) -> PresenceSummary {
        guard s.connection.isLive else {
            let why: String
            switch s.connection {
            case .disconnected(let reason): why = reason ?? "offline"
            default: why = "connecting"
            }
            return PresenceSummary(label: "Mac unreachable: \(why)", control: .none, canSend: false)
        }
        if let h = s.handoff {
            let into = h.to == s.surface
            return PresenceSummary(label: into ? "Jacking in…" : "Jacking out…", control: .none,
                                   canSend: false)
        }
        if s.isActiveSurface {
            return PresenceSummary(label: "Stark is here", control: .jackOut, canSend: true)
        }
        return PresenceSummary(label: "Stark is on \(s.holderName ?? "another surface")",
                               control: .jackIn, canSend: false)
    }
}
