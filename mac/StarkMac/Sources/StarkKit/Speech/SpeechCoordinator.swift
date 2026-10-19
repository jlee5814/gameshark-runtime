import Foundation
import Observation

/// Stark's voice on one surface: feeds published states to SpeechMachine and carries out
/// its effects on an engine and a store. It holds no GatewayClient, so speech can never
/// send a command, write memory, or start inference.
@MainActor @Observable
public final class SpeechCoordinator {
    /// The row being read aloud; nil when Stark is silent.
    public private(set) var speakingSeq: Int?
    public private(set) var settings: VoiceSettings
    /// Replay is offered only where Stark could speak on his own.
    public private(set) var canReplay = false

    @ObservationIgnored private var machine: SpeechMachine
    @ObservationIgnored private let engine: any SpeechEngine
    @ObservationIgnored private let store: any SpeechStore
    /// Diagnostic lines (SpeechDiagnostic): ids and reasons, never a row's text.
    @ObservationIgnored private let trace: Trace?

    public init(engine: any SpeechEngine, store: any SpeechStore, trace: Trace? = nil) {
        self.engine = engine
        self.store = store
        self.trace = trace
        let saved = store.data(SpeechLedger.settingsKey)
            .flatMap { try? JSONDecoder().decode(VoiceSettings.self, from: $0) }
        let v = (saved ?? VoiceSettings()).clamped()
        settings = v
        machine = SpeechMachine(muted: v.muted)
        engine.onFinish = { [weak self] id in self?.apply(.finished(id)) }
        engine.onInterrupt = { [weak self] in self?.apply(.interrupted, cause: .audio) }
        log(.ready(muted: v.muted, voice: v.voiceIdentifier))
    }

    public func observe(_ s: ClientState) { apply(.state(s)) }

    public func setMuted(_ muted: Bool) {
        update { $0.muted = muted }
    }

    public func toggleMute() { setMuted(!settings.muted) }

    public func replay(seq: Int) { apply(.replay(seq)) }

    /// Off screen, Stark is silent; on return, replies that landed meanwhile stay unspoken.
    public func setSuspended(_ hidden: Bool) { apply(.setSuspended(hidden)) }

    /// Stop button and avatar tap.
    public func stop() { apply(.stop, cause: .user) }

    /// The user sent another message: Stark stops talking over them.
    public func userSent() { apply(.stop, cause: .send) }

    /// The audio output went away outside the engine's view (the Mac going to sleep).
    public func interrupted() { apply(.interrupted, cause: .system) }

    public func update(_ change: (inout VoiceSettings) -> Void) {
        var v = settings
        change(&v)
        v = v.clamped()
        guard v != settings else { return }
        settings = v
        if let blob = try? JSONEncoder().encode(v) { store.setData(blob, SpeechLedger.settingsKey) }
        if v.muted != machine.muted { apply(.setMuted(v.muted)) }
    }

    private func apply(_ e: SpeechMachine.Event, cause: SpeechDiagnostic.Cause? = nil) {
        let store = self.store
        let before = machine
        let fx = machine.step(e, load: { store.integer($0) })
        if trace != nil { explain(e, before: before, cause: cause) }
        for f in fx {
            switch f {
            case .persist(let key, let seq):
                store.setInteger(seq, key)
            case .stopEngine:
                engine.stop()
            case .speak(let id, let text):
                if trace != nil {
                    let origin: SpeechDiagnostic.Origin
                    if case .replay = e { origin = .replay } else { origin = .auto }
                    log(.accepted(id, origin: origin, chars: text.count))
                }
                engine.speak(Utterance(id: id, text: text, voice: settings))
            }
        }
        speakingSeq = machine.speaking?.seq
        canReplay = machine.isArmed
    }

    /// Logs what one step decided, comparing the machine before and after; gate lines
    /// appear only on a change, so a steady stream of states logs nothing.
    private func explain(_ e: SpeechMachine.Event, before: SpeechMachine,
                         cause: SpeechDiagnostic.Cause?) {
        if let cause { log(.stopped(cause, playing: before.speaking)) }
        switch e {
        case .setMuted(let m) where m != before.muted:
            log(.muted(m))
        case .setSuspended(let s) where s != before.suspended:
            log(.suspended(s))
        case .finished(let id):
            log(before.speaking == id ? .finished(id) : .staleFinish(id))
        default:
            break
        }
        switch machine.lastDecision {
        case .skippedMuted(let seq): log(.skippedMuted(seq: seq))
        case .replayRejected(let seq, let why): log(.rejected(seq: seq, why))
        case nil: break
        }
        guard machine.gate != before.gate else { return }
        switch machine.gate {
        case .armed(let k):
            log(.armed(agent: k.uuid, surface: k.surface, generation: k.generation,
                       handled: machine.handled))
        case .disarmed(let why):
            log(.disarmed(why))
        }
    }

    private func log(_ d: SpeechDiagnostic) { trace?(d.line) }
}
