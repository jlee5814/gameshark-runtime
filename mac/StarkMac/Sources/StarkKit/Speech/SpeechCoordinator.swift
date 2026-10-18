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

    public init(engine: any SpeechEngine, store: any SpeechStore) {
        self.engine = engine
        self.store = store
        let saved = store.data(SpeechLedger.settingsKey)
            .flatMap { try? JSONDecoder().decode(VoiceSettings.self, from: $0) }
        let v = (saved ?? VoiceSettings()).clamped()
        settings = v
        machine = SpeechMachine(muted: v.muted)
        engine.onFinish = { [weak self] id in self?.apply(.finished(id)) }
        engine.onInterrupt = { [weak self] in self?.apply(.interrupted) }
    }

    public func observe(_ s: ClientState) { apply(.state(s)) }

    public func setMuted(_ muted: Bool) {
        update { $0.muted = muted }
    }

    public func toggleMute() { setMuted(!settings.muted) }

    public func replay(seq: Int) { apply(.replay(seq)) }

    /// Stop button and avatar tap.
    public func stop() { apply(.stop) }

    /// The user sent another message: Stark stops talking over them.
    public func userSent() { apply(.stop) }

    /// The audio output went away outside the engine's view (the Mac going to sleep).
    public func interrupted() { apply(.interrupted) }

    public func update(_ change: (inout VoiceSettings) -> Void) {
        var v = settings
        change(&v)
        v = v.clamped()
        guard v != settings else { return }
        settings = v
        if let blob = try? JSONEncoder().encode(v) { store.setData(blob, SpeechLedger.settingsKey) }
        if v.muted != machine.muted { apply(.setMuted(v.muted)) }
    }

    private func apply(_ e: SpeechMachine.Event) {
        let store = self.store
        let fx = machine.step(e, load: { store.integer($0) })
        for f in fx {
            switch f {
            case .persist(let key, let seq):
                store.setInteger(seq, key)
            case .stopEngine:
                engine.stop()
            case .speak(let id, let text):
                engine.speak(Utterance(id: id, text: text, voice: settings))
            }
        }
        speakingSeq = machine.speaking?.seq
        canReplay = machine.isArmed
    }
}
