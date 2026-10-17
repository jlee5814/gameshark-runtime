import Foundation

/// What the avatar shows, derived from runtime state plus two local UI facts.
public enum AvatarState: String, CaseIterable, Sendable {
    case disconnected   // no live session with the runtime
    case ghost          // connected, but another surface holds presence: render, never act
    case working        // Stark is executing: a reply in flight or a duty running
    case listening      // the user is typing to Stark
    case notification   // a reply arrived that the user has not viewed
    case idle

    /// Precedence runs top to bottom: a dead link outranks everything, and a surface that
    /// lost presence never claims to be working for the user.
    public static func derive(_ s: ClientState, listening: Bool, viewedSeq: Int) -> AvatarState {
        if !s.connection.isLive { return .disconnected }
        if !s.isActiveSurface { return .ghost }
        if s.isWorking || s.isThinking { return .working }
        // Queued or waiting for the model is still work on the user's message; a failed
        // reply is not, and shows in the conversation instead.
        if s.reply?.pending == true { return .working }
        if listening { return .listening }
        let newest = s.rows.last { $0.role == "assistant" }?.seq ?? -1
        if newest > viewedSeq { return .notification }
        return .idle
    }
}
