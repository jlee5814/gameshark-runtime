import Foundation
import Observation

/// Push to talk on one surface: feeds published states and the user's presses to
/// CaptureMachine and carries out its effects on a SpeechInput and Stark's voice. It holds
/// no GatewayClient: the voice draft reaches the runtime only through `submit`, which the
/// model runs when the user presses Send, and the composer only through `insert`.
@MainActor @Observable
public final class CaptureCoordinator {
    public private(set) var phase: CaptureMachine.Phase = .idle
    /// The voice draft, editable in review; local to this device and never persisted.
    public private(set) var draft = ""
    public private(set) var availability: InputAvailability
    /// Push to talk works only where Stark is.
    public private(set) var isPresent = false
    /// Why the last attempt did nothing or the last draft went away, in words for the user.
    public private(set) var notice: String?

    /// How long to wait for a final transcript after release before keeping what was heard.
    public static let finishTimeout = Duration.seconds(3)

    @ObservationIgnored private var machine = CaptureMachine()
    @ObservationIgnored private let input: any SpeechInput
    @ObservationIgnored private let speech: SpeechCoordinator
    @ObservationIgnored private let submit: (String) -> Bool
    @ObservationIgnored private let insert: (String) -> Void
    @ObservationIgnored private let trace: Trace?

    /// `submit` runs the surface's own send path and says whether it accepted the text.
    public init(input: any SpeechInput, speech: SpeechCoordinator, trace: Trace? = nil,
                submit: @escaping (String) -> Bool, insert: @escaping (String) -> Void) {
        self.input = input
        self.speech = speech
        self.trace = trace
        self.submit = submit
        self.insert = insert
        availability = input.availability
        input.availabilityChanged = { [weak self] in self?.refresh() }
    }

    public var isListening: Bool { machine.isListening }

    /// Whether a press would start listening now; the control stays pressable whenever
    /// Stark is here, so a press can read access again after a change in Settings.
    public var canPress: Bool {
        guard isPresent else { return false }
        switch availability {
        case .ready, .needsPermission: return phase == .idle || phase == .reviewing
        default: return false
        }
    }

    public func observe(_ s: ClientState) { apply(.state(s)) }

    /// Read availability again (the user may have changed Settings).
    public func refresh() { availability = input.availability }

    public func press() {
        refresh()
        notice = nil
        apply(.press(availability))
    }

    public func release() { apply(.release) }

    public func cancel(_ why: CaptureMachine.Reason = .user) { apply(.cancel(why)) }

    /// End an open microphone only; a permission prompt or a reviewed draft stays.
    public func stopListening(_ why: CaptureMachine.Reason) {
        guard isListening else { return }
        apply(.cancel(why))
    }

    public func edit(_ text: String) { apply(.edit(text)) }

    /// Submit the reviewed text once; the voice draft clears only when the surface took it.
    public func send() {
        guard phase == .reviewing else { return }
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, submit(text) else { return }
        apply(.sent)
    }

    /// Move the reviewed text into the ordinary composer, where draft continuity applies.
    public func insertIntoComposer() {
        guard phase == .reviewing else { return }
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        insert(text)
        apply(.inserted)
    }

    private func apply(_ e: CaptureMachine.Event) {
        let fx = machine.step(e)
        if let d = machine.lastDecision {
            trace?(SpeechDiagnostic.capture(d).line)
            notice = Self.notice(d) ?? notice
        }
        for f in fx {
            switch f {
            case .holdSpeech:
                speech.setListening(true)
            case .releaseSpeech:
                speech.setListening(false)
            case .requestPermission:
                input.requestPermission { [weak self] result in
                    self?.availability = result
                    self?.apply(.permission(result))
                }
            case .startRecognizer(let n):
                input.start(session: n) { [weak self] n, event in self?.apply(.input(n, event)) }
            case .finishRecognizer(let n):
                input.finish()
                Task { [weak self] in
                    try? await Task.sleep(for: Self.finishTimeout)
                    self?.apply(.finishTimeout(n))
                }
            case .cancelRecognizer:
                input.cancel()
            }
        }
        phase = machine.phase
        draft = machine.draft
        isPresent = machine.isPresent
    }

    private static func notice(_ d: CaptureMachine.Decision) -> String? {
        switch d {
        case .rejected(.notPresent): "Jack In to talk to Stark here."
        case .rejected(.availability(let a)): a.explanation
        case .permission(.ready): "Ready. Hold the microphone to talk."
        case .permission(let a): a.explanation
        case .discarded(.presence, _): "Voice draft discarded: Stark moved or the link dropped."
        case .discarded(.interrupted, _): "Voice draft discarded: the microphone was interrupted."
        case .discarded(.failed, _): "Stark didn't catch that."
        case .discarded(.empty, _): "Stark didn't catch that."
        case .discarded(.background, _), .discarded(.sleep, _), .discarded(.inactive, _):
            "Voice draft discarded when the app left the screen."
        default: nil
        }
    }
}
