import Foundation
#if os(iOS)
import AVFoundation
#endif

/// The two configurations Stark asks of the shared audio session.
public enum AudioSessionConfig: Equatable, Sendable {
    /// Speech alone: `.playback`, `.spokenAudio`, ducking others.
    case playback
    /// Push to talk: `.record`, `.measurement`. Recording already silences other audio;
    /// `.duckOthers` is not valid with `.record`.
    case record
}

/// The parts of AVAudioSession the claims touch; a fake in tests.
@MainActor
public protocol AudioSessionPort: AnyObject {
    func setCategory(_ config: AudioSessionConfig) throws
    func setActive(_ active: Bool) throws
}

/// One owner for the shared audio session. Speech output and push to talk each claim it;
/// the session is active while any claim holds and deactivates only when the last one
/// releases, so a stopped utterance finishing late never shuts the microphone off.
@MainActor
public final class AudioSessionClaims {
    public enum Claim: Hashable, Sendable { case speech, capture }

    private let port: any AudioSessionPort
    public private(set) var claims: Set<Claim> = []
    public private(set) var active = false
    private var config: AudioSessionConfig?

    public init(port: any AudioSessionPort) { self.port = port }

    #if os(iOS)
    public static let shared = AudioSessionClaims(port: SystemAudioSession())
    #endif

    /// Take the session for `claim`; false when it could not be activated, or for capture
    /// when the record category could not be set, in which case the claim is not held.
    /// Speech keeps its Phase 4.3B rule: a category failure is logged and play goes on.
    /// Returns what happened, for the speech diagnostics.
    public func acquire(_ claim: Claim) -> (ok: Bool, events: [SpeechDiagnostic.Session]) {
        var events: [SpeechDiagnostic.Session] = []
        let want: AudioSessionConfig = claims.contains(.capture) || claim == .capture
            ? .record : .playback
        if !active || want != config {
            do {
                try port.setCategory(want)
                config = want
            } catch {
                events.append(.categoryFailed(code: (error as NSError).code))
                if want == .record { return (false, events) }
            }
        }
        if !active {
            do {
                try port.setActive(true)
                active = true
                events.append(.activated)
            } catch {
                events.append(.activateFailed(code: (error as NSError).code))
                return (false, events)
            }
        }
        claims.insert(claim)
        return (true, events)
    }

    /// Give up `claim`; the session deactivates, notifying others, once no claim is left.
    /// While another claim remains, the session is set back to what that claim needs: a
    /// capture ending before a stopped utterance reports its end leaves playback, not record.
    public func release(_ claim: Claim) -> [SpeechDiagnostic.Session] {
        guard claims.remove(claim) != nil, active else { return [] }
        guard claims.isEmpty else {
            let want: AudioSessionConfig = claims.contains(.capture) ? .record : .playback
            guard want != config else { return [] }
            do {
                try port.setCategory(want)
                config = want
                return []
            } catch {
                return [.categoryFailed(code: (error as NSError).code)]
            }
        }
        active = false
        config = nil
        do {
            try port.setActive(false)
            return [.deactivated]
        } catch {
            return [.deactivateFailed(code: (error as NSError).code)]
        }
    }
}

#if os(iOS)
/// The real shared AVAudioSession.
@MainActor
final class SystemAudioSession: AudioSessionPort {
    func setCategory(_ config: AudioSessionConfig) throws {
        let s = AVAudioSession.sharedInstance()
        switch config {
        case .playback: try s.setCategory(.playback, mode: .spokenAudio, options: [.duckOthers])
        case .record: try s.setCategory(.record, mode: .measurement, options: [])
        }
    }

    func setActive(_ active: Bool) throws {
        if active {
            try AVAudioSession.sharedInstance().setActive(true)
        } else {
            try AVAudioSession.sharedInstance().setActive(false,
                                                          options: .notifyOthersOnDeactivation)
        }
    }
}
#endif
