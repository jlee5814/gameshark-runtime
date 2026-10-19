import AVFoundation
import Foundation
import Speech

/// The shipped recognizer: Apple's Speech framework, on device only. Audio buffers go from
/// the microphone tap straight to the recognizer in memory; nothing is written to a file,
/// and a recognizer that cannot run on device is reported unavailable, never used through
/// the server.
///
/// iOS: push to talk claims the shared session through AudioSessionClaims, so Stark's
/// voice output and the microphone never deactivate each other. An interruption, a lost
/// route, or a media server reset ends the capture through `.interrupted`.
@MainActor
public final class SFSpeechInput: NSObject, SpeechInput {
    /// Stark speaks English whatever the system language is.
    public static let locale = Locale(identifier: "en-US")

    /// Rebuilt after a media services reset, which orphans every audio object.
    private var recognizer = SFSpeechRecognizer(locale: SFSpeechInput.locale)
    private var engine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var tapped = false
    /// The capture whose events are still wanted; nil once cancelled.
    private var active: Int?
    private var sink: (@MainActor (Int, InputEvent) -> Void)?
    private var observers: [NSObjectProtocol] = []
    private let trace: Trace?
    #if os(iOS)
    private var claimed = false
    #endif
    public var availabilityChanged: (@MainActor () -> Void)?

    public init(trace: Trace? = nil) {
        self.trace = trace
        super.init()
        recognizer?.delegate = self
        watchAudio()
    }

    public var availability: InputAvailability {
        let info = Bundle.main.infoDictionary ?? [:]
        guard info["NSMicrophoneUsageDescription"] != nil,
              info["NSSpeechRecognitionUsageDescription"] != nil
        else { return .unavailable(.notConfigured) }
        guard let recognizer else { return .unavailable(.noRecognizer) }
        guard recognizer.supportsOnDeviceRecognition else { return .unavailable(.noOnDevice) }
        let speech = Self.speechPermission()
        let mic = Self.micPermission()
        if speech == .denied || mic == .denied { return .denied }
        if speech == .restricted || mic == .restricted { return .restricted }
        if speech == .needsPermission || mic == .needsPermission { return .needsPermission }
        guard recognizer.isAvailable else { return .unavailable(.offline) }
        return .ready
    }

    public func requestPermission(_ done: @escaping @MainActor (InputAvailability) -> Void) {
        Task { @MainActor [weak self] in
            if Self.speechPermission() == .needsPermission { _ = await Self.askSpeech() }
            if Self.micPermission() == .needsPermission { _ = await Self.askMic() }
            done(self?.availability ?? .denied)
        }
    }

    public func start(session: Int, _ events: @escaping @MainActor (Int, InputEvent) -> Void) {
        cancel()
        guard let recognizer, recognizer.supportsOnDeviceRecognition else {
            fail(session, -1, events)
            return
        }
        #if os(iOS)
        let (ok, sessionEvents) = AudioSessionClaims.shared.acquire(.capture)
        for e in sessionEvents { trace?(SpeechDiagnostic.session(e).line) }
        guard ok else {
            fail(session, -2, events)
            return
        }
        claimed = true
        #endif
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.requiresOnDeviceRecognition = true
        request.shouldReportPartialResults = true
        request.addsPunctuation = true
        request.taskHint = .dictation
        request.contextualStrings = ["Stark", "GameShark", "Jack In", "Jack Out"]
        let node = engine.inputNode
        let format = node.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            teardown()
            fail(session, -3, events)
            return
        }
        node.installTap(onBus: 0, bufferSize: 1024, format: format,
                        block: Self.feed(RequestBox(request)))
        tapped = true
        engine.prepare()
        do {
            try engine.start()
        } catch {
            teardown()
            fail(session, (error as NSError).code, events)
            return
        }
        self.request = request
        active = session
        sink = events
        task = recognizer.recognitionTask(with: request,
                                          resultHandler: Self.results(session, deliver: deliver))
    }

    /// A start that could not open the microphone reports after the caller's step ends,
    /// as a recognizer would, never from inside it.
    private func fail(_ session: Int, _ code: Int,
                      _ events: @escaping @MainActor (Int, InputEvent) -> Void) {
        trace?("capture input failed code=\(code)")
        Task { @MainActor in events(session, .failed(code: code)) }
    }

    public func finish() {
        stopMicrophone()
        request?.endAudio()
    }

    public func cancel() {
        active = nil
        sink = nil
        teardown()
    }

    /// Hops a recognizer callback to the main actor; drops it unless its capture is current.
    private var deliver: @Sendable (Int, InputEvent) -> Void {
        { [weak self] n, event in
            Task { @MainActor in
                guard let self, self.active == n, let sink = self.sink else { return }
                if case .final = event { self.active = nil }
                sink(n, event)
            }
        }
    }

    private func stopMicrophone() {
        if engine.isRunning { engine.stop() }
        if tapped {
            engine.inputNode.removeTap(onBus: 0)
            tapped = false
        }
    }

    private func teardown() {
        task?.cancel()
        task = nil
        stopMicrophone()
        request?.endAudio()
        request = nil
        #if os(iOS)
        if claimed {
            claimed = false
            for e in AudioSessionClaims.shared.release(.capture) {
                trace?(SpeechDiagnostic.session(e).line)
            }
        }
        #endif
    }

    private func lost() {
        guard let n = active, let sink else { return }
        sink(n, .interrupted)
    }

    /// End any capture, then replace the orphaned engine and recognizer so the next press
    /// works without a relaunch.
    private func rebuild() {
        lost()
        cancel()
        engine = AVAudioEngine()
        recognizer = SFSpeechRecognizer(locale: Self.locale)
        recognizer?.delegate = self
        trace?("capture input rebuilt after media reset")
        availabilityChanged?()
    }

    /// The microphone or route going away mid capture ends it; nothing restarts.
    private func watchAudio() {
        let nc = NotificationCenter.default
        observers.append(nc.addObserver(forName: .AVAudioEngineConfigurationChange,
                                        object: nil, queue: .main) { [weak self] note in
            let changed = (note.object as? AVAudioEngine).map(ObjectIdentifier.init)
            MainActor.assumeIsolated {
                guard let self, changed == ObjectIdentifier(self.engine) else { return }
                self.lost()
            }
        })
        #if os(iOS)
        let s = AVAudioSession.sharedInstance()
        observers.append(nc.addObserver(forName: AVAudioSession.interruptionNotification,
                                        object: s, queue: .main) { [weak self] note in
            let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            let began = raw.flatMap(AVAudioSession.InterruptionType.init) == .began
            MainActor.assumeIsolated { if began { self?.lost() } }
        })
        observers.append(nc.addObserver(forName: AVAudioSession.routeChangeNotification,
                                        object: s, queue: .main) { [weak self] note in
            let raw = note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt
            let gone = raw.flatMap(AVAudioSession.RouteChangeReason.init) == .oldDeviceUnavailable
            MainActor.assumeIsolated { if gone { self?.lost() } }
        })
        observers.append(nc.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification,
                                        object: s, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.rebuild() }
        })
        #endif
    }

    // MARK: callbacks off the main actor

    /// The tap runs on the audio thread, so it is built outside main actor isolation.
    private nonisolated static func feed(_ box: RequestBox) -> AVAudioNodeTapBlock {
        { buffer, _ in box.request.append(buffer) }
    }

    private nonisolated static func results(
        _ n: Int, deliver: @escaping @Sendable (Int, InputEvent) -> Void
    ) -> @Sendable (SFSpeechRecognitionResult?, (any Error)?) -> Void {
        { result, error in
            if let result {
                let text = result.bestTranscription.formattedString
                deliver(n, result.isFinal ? .final(text) : .partial(text))
            } else if let error {
                deliver(n, .failed(code: (error as NSError).code))
            }
        }
    }

    // MARK: permissions

    private enum Permission { case granted, needsPermission, denied, restricted }

    private nonisolated static func speechPermission() -> Permission {
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized: .granted
        case .denied: .denied
        case .restricted: .restricted
        default: .needsPermission
        }
    }

    private nonisolated static func micPermission() -> Permission {
        #if os(iOS)
        switch AVAudioApplication.shared.recordPermission {
        case .granted: .granted
        case .denied: .denied
        default: .needsPermission
        }
        #else
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: .granted
        case .denied: .denied
        case .restricted: .restricted
        default: .needsPermission
        }
        #endif
    }

    private nonisolated static func askSpeech() async -> Bool {
        await withCheckedContinuation { c in
            SFSpeechRecognizer.requestAuthorization { c.resume(returning: $0 == .authorized) }
        }
    }

    private nonisolated static func askMic() async -> Bool {
        #if os(iOS)
        await AVAudioApplication.requestRecordPermission()
        #else
        await AVCaptureDevice.requestAccess(for: .audio)
        #endif
    }
}

extension SFSpeechInput: SFSpeechRecognizerDelegate {
    public nonisolated func speechRecognizer(_ r: SFSpeechRecognizer,
                                             availabilityDidChange available: Bool) {
        Task { @MainActor in self.availabilityChanged?() }
    }
}

/// Carries the request into the audio thread's tap; append is safe from any thread.
private final class RequestBox: @unchecked Sendable {
    let request: SFSpeechAudioBufferRecognitionRequest
    init(_ request: SFSpeechAudioBufferRecognitionRequest) { self.request = request }
}
