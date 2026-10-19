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
    /// No finish or cancel ever arrived; the probe was dropped.
    case noEnd(Utterance.ID, afterMs: Int)
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
        case .noEnd(let id, let afterMs):
            "synth no_end \(Self.name(id)) after_ms=\(afterMs)"
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

/// Synthesizer timing per utterance object, for diagnostics only. Kept per object rather
/// than for the current utterance alone, because a superseded utterance reports its cancel
/// after the next one has started. An utterance that never reports an end is dropped by
/// `expire` after `retention`, which the engine schedules per utterance. A probe keeps its utterance object alive, so while it exists no other
/// utterance can take the same object identifier and a late callback finds its own probe.
struct UtteranceProbes<Key: Hashable> {
    struct Probe {
        var id: Utterance.ID
        var at: ContinuousClock.Instant
        var started = false
        var object: AnyObject?
    }

    static var retention: Duration { .seconds(600) }

    private(set) var probes: [Key: Probe] = [:]

    mutating func spoke(_ key: Key, _ id: Utterance.ID, at now: ContinuousClock.Instant,
                        keeping object: AnyObject? = nil) {
        probes[key] = Probe(id: id, at: now, object: object)
    }

    mutating func started(_ key: Key, at now: ContinuousClock.Instant) -> SpeechDiagnostic? {
        guard let p = probes[key], !p.started else { return nil }
        probes[key]?.started = true
        return .synthStarted(p.id, ms: Self.ms(now - p.at))
    }

    mutating func ended(_ key: Key, cancelled: Bool,
                        at now: ContinuousClock.Instant) -> SpeechDiagnostic? {
        guard let p = probes.removeValue(forKey: key) else { return nil }
        let ms = Self.ms(now - p.at)
        return cancelled ? .synthCancelled(p.id, ms: ms, started: p.started)
                         : .synthFinished(p.id, ms: ms, started: p.started)
    }

    /// `id` binds a watchdog to its own utterance: an object identifier can be reused once
    /// an earlier utterance is gone.
    func noStart(_ key: Key, _ id: Utterance.ID, voice: String?,
                 at now: ContinuousClock.Instant) -> SpeechDiagnostic? {
        guard let p = probes[key], p.id == id, !p.started else { return nil }
        return .noStart(p.id, afterMs: Self.ms(now - p.at), voice: voice)
    }

    /// Drops a probe whose utterance never ended; `id` binds the call to its own utterance.
    mutating func expire(_ key: Key, _ id: Utterance.ID,
                         at now: ContinuousClock.Instant) -> SpeechDiagnostic? {
        guard let p = probes[key], p.id == id else { return nil }
        probes[key] = nil
        return .noEnd(p.id, afterMs: Self.ms(now - p.at))
    }

    static func ms(_ d: Duration) -> Int {
        Int(d.components.seconds * 1000 + d.components.attoseconds / 1_000_000_000_000_000)
    }
}
