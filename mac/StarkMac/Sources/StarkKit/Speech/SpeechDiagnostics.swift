import Foundation

/// One speech fact for the device log. No case carries a row's text, so a line can never
/// repeat what Stark said: seq and tok tie lines to a reply and to one reading of it, and
/// chars gives only its length. Voice identifiers name installed system voices.
public enum SpeechDiagnostic: Equatable, Sendable {
    public enum Origin: String, Sendable { case auto, replay }

    /// Who silenced Stark: the Stop control, a sent message, the engine losing its audio,
    /// or the app reporting the output gone (the Mac going to sleep).
    public enum Cause: String, Sendable { case user, send, audio, system }

    public enum Rejection: Equatable, Sendable {
        case disarmed(SpeechMachine.Disarm)
        case unknownRow
    }

    public enum Session: Equatable, Sendable {
        case categoryFailed(code: Int)
        case activated
        case activateFailed(code: Int)
        case deactivated
        case deactivateFailed(code: Int)
        case interruptionBegan
        case interruptionEnded
        case route(reason: UInt)
        case mediaReset
    }

    public enum Lifecycle: String, Sendable { case sleep, wake }

    /// The coordinator came up with these settings.
    case ready(muted: Bool, voice: String?)
    /// What this device offers: the default voice, offered English voices, all installed.
    case catalog(defaultVoice: String?, offered: Int, installed: Int)
    case armed(agent: String, surface: String, generation: Int, handled: Int)
    case disarmed(SpeechMachine.Disarm)
    case accepted(Utterance.ID, origin: Origin, chars: Int)
    case skippedMuted(seq: Int)
    case rejected(seq: Int, Rejection)
    case stopped(Cause, playing: Utterance.ID?)
    case suspended(Bool)
    case muted(Bool)
    case finished(Utterance.ID)
    case staleFinish(Utterance.ID)
    case voice(Utterance.ID, requested: String?, resolved: String?, name: String?,
               quality: Int?, how: VoiceCatalog.How)
    case synthStarted(Utterance.ID, ms: Int)
    case synthFinished(Utterance.ID, ms: Int, started: Bool)
    case synthCancelled(Utterance.ID, ms: Int, started: Bool)
    /// Speak was issued but the synthesizer never began: the silent playback signature.
    case noStart(Utterance.ID, afterMs: Int, voice: String?)
    case session(Session)
    case voicesChanged(installed: Int)
    case lifecycle(Lifecycle)

    public var line: String {
        switch self {
        case .ready(let muted, let voice):
            "ready muted=\(muted) voice=\(voice ?? "default")"
        case .catalog(let d, let offered, let installed):
            "catalog default=\(d ?? "none") offered=\(offered) installed=\(installed)"
        case .armed(let agent, let surface, let generation, let handled):
            "armed agent=\(agent.prefix(8)) surface=\(surface) gen=\(generation) handled=\(handled)"
        case .disarmed(let why):
            "disarmed reason=\(Self.name(why))"
        case .accepted(let id, let origin, let chars):
            "speak \(Self.name(id)) origin=\(origin.rawValue) chars=\(chars)"
        case .skippedMuted(let seq):
            "skip seq=\(seq) reason=muted"
        case .rejected(let seq, .disarmed(let why)):
            "replay rejected seq=\(seq) reason=\(Self.name(why))"
        case .rejected(let seq, .unknownRow):
            "replay rejected seq=\(seq) reason=unknown_row"
        case .stopped(let cause, let playing):
            "stop by=\(cause.rawValue) \(playing.map(Self.name) ?? "idle")"
        case .suspended(let on):
            "suspended \(on ? "on" : "off")"
        case .muted(let on):
            "muted \(on ? "on" : "off")"
        case .finished(let id):
            "finished \(Self.name(id))"
        case .staleFinish(let id):
            "finish stale \(Self.name(id))"
        case .voice(let id, let requested, let resolved, let name, let quality, let how):
            "voice \(Self.name(id)) requested=\(requested ?? "default") "
                + "resolved=\(resolved ?? "none") name=\(name ?? "none") "
                + "quality=\(quality.map(String.init) ?? "none") how=\(how.rawValue)"
        case .synthStarted(let id, let ms):
            "synth start \(Self.name(id)) ms=\(ms)"
        case .synthFinished(let id, let ms, let started):
            "synth finish \(Self.name(id)) ms=\(ms) started=\(started)"
        case .synthCancelled(let id, let ms, let started):
            "synth cancel \(Self.name(id)) ms=\(ms) started=\(started)"
        case .noStart(let id, let afterMs, let voice):
            "synth no_start \(Self.name(id)) after_ms=\(afterMs) voice=\(voice ?? "none")"
        case .session(let s):
            "session " + Self.name(s)
        case .voicesChanged(let installed):
            "voices changed installed=\(installed)"
        case .lifecycle(let l):
            "system \(l.rawValue)"
        }
    }

    static func name(_ id: Utterance.ID) -> String { "seq=\(id.seq) tok=\(id.token)" }

    static func name(_ why: SpeechMachine.Disarm) -> String {
        switch why {
        case .noIdentity: "no_identity"
        case .offline: "offline"
        case .ghost: "ghost"
        case .handoff: "handoff"
        case .suspended: "suspended"
        }
    }

    static func name(_ s: Session) -> String {
        switch s {
        case .categoryFailed(let code): "category failed code=\(code)"
        case .activated: "activate ok"
        case .activateFailed(let code): "activate failed code=\(code)"
        case .deactivated: "deactivate ok"
        case .deactivateFailed(let code): "deactivate failed code=\(code)"
        case .interruptionBegan: "interruption began"
        case .interruptionEnded: "interruption ended"
        case .route(let reason): "route reason=\(reason)"
        case .mediaReset: "media_reset"
        }
    }
}
