import AVFoundation
import Foundation

/// The on device engine: AVSpeechSynthesizer, offline, no audio leaves the device.
///
/// iOS: a `.playback` / `.spokenAudio` session ducks other audio while Stark talks and is
/// released after. An interruption, a lost output route, or a media server reset ends the
/// utterance through onInterrupt; nothing resumes on its own.
///
/// With a trace, it logs SpeechDiagnostic lines: the voice each utterance resolved to,
/// when the synthesizer started and ended, a speak that never started, and session events.
@MainActor
public final class AVSpeechEngine: NSObject, SpeechEngine {
    public var onFinish: ((Utterance.ID) -> Void)?
    public var onInterrupt: (() -> Void)?

    /// A speak with no start after this long is logged once; nothing else happens.
    nonisolated static let startWatchdog = Duration.seconds(3)

    private let synth = AVSpeechSynthesizer()
    private var current: (ObjectIdentifier, Utterance.ID)?
    private var observers: [NSObjectProtocol] = []
    /// The iOS audio session is ours right now; released only when it was taken.
    private var sessionActive = false
    private let trace: Trace?
    /// When each utterance was handed over and whether it began; diagnostics only.
    private var probes = UtteranceProbes<ObjectIdentifier>()

    public init(trace: Trace? = nil) {
        self.trace = trace
        super.init()
        synth.delegate = self
        #if os(iOS)
        synth.usesApplicationAudioSession = true
        watchSession()
        #endif
        if trace != nil { watchVoices() }
    }

    public func speak(_ u: Utterance) {
        if synth.isSpeaking || synth.isPaused { synth.stopSpeaking(at: .immediate) }
        let v = u.voice.clamped()
        let say = AVSpeechUtterance(string: u.text)
        let (voice, how) = VoiceCatalog.resolve(v.voiceIdentifier)
        say.voice = voice
        say.rate = Float(min(max(Double(AVSpeechUtteranceDefaultSpeechRate) * v.rate,
                                 Double(AVSpeechUtteranceMinimumSpeechRate)),
                             Double(AVSpeechUtteranceMaximumSpeechRate)))
        say.pitchMultiplier = Float(v.pitch)
        current = (ObjectIdentifier(say), u.id)
        if trace != nil {
            log(.voice(u.id, requested: v.voiceIdentifier, resolved: voice?.identifier,
                       name: voice?.name, quality: voice.map { VoiceCatalog.quality($0.quality) },
                       how: how))
            let which = ObjectIdentifier(say)
            probes.spoke(which, u.id, at: .now, keeping: say)
            Task { [weak self] in
                try? await Task.sleep(for: Self.startWatchdog)
                self?.checkStarted(which, u.id, voice: voice?.identifier)
            }
            Task { [weak self] in
                try? await Task.sleep(for: UtteranceProbes<ObjectIdentifier>.retention)
                self?.expire(which, u.id)
            }
        }
        activateSession()
        synth.speak(say)
    }

    public func stop() {
        guard synth.isSpeaking || synth.isPaused else {
            deactivateSession()
            return
        }
        synth.stopSpeaking(at: .immediate)
    }

    private func ended(_ which: ObjectIdentifier, cancelled: Bool) {
        if let d = probes.ended(which, cancelled: cancelled, at: .now) { log(d) }
        guard let (obj, id) = current, obj == which else { return }
        current = nil
        deactivateSession()
        onFinish?(id)
    }

    private func began(_ which: ObjectIdentifier) {
        if let d = probes.started(which, at: .now) { log(d) }
    }

    private func checkStarted(_ which: ObjectIdentifier, _ id: Utterance.ID, voice: String?) {
        if let d = probes.noStart(which, id, voice: voice, at: .now) { log(d) }
    }

    private func expire(_ which: ObjectIdentifier, _ id: Utterance.ID) {
        if let d = probes.expire(which, id, at: .now) { log(d) }
    }

    private func activateSession() {
        #if os(iOS)
        guard !sessionActive else { return }
        let s = AVAudioSession.sharedInstance()
        do {
            try s.setCategory(.playback, mode: .spokenAudio, options: [.duckOthers])
        } catch {
            log(.session(.categoryFailed(code: (error as NSError).code)))
        }
        do {
            try s.setActive(true)
            sessionActive = true
            log(.session(.activated))
        } catch {
            sessionActive = false
            log(.session(.activateFailed(code: (error as NSError).code)))
        }
        #endif
    }

    private func deactivateSession() {
        #if os(iOS)
        guard sessionActive, !synth.isSpeaking else { return }
        sessionActive = false
        do {
            try AVAudioSession.sharedInstance().setActive(false,
                                                          options: .notifyOthersOnDeactivation)
            log(.session(.deactivated))
        } catch {
            log(.session(.deactivateFailed(code: (error as NSError).code)))
        }
        #endif
    }

    private func log(_ d: SpeechDiagnostic) { trace?(d.line) }

    /// Installed voices changing at run time (a download, the speech extension reloading)
    /// is a lead for a voice that resolves but never sounds.
    private func watchVoices() {
        log(.catalog(defaultVoice: VoiceCatalog.defaultOption?.id,
                     offered: VoiceCatalog.options().count,
                     installed: AVSpeechSynthesisVoice.speechVoices().count))
        observers.append(NotificationCenter.default.addObserver(
            forName: AVSpeechSynthesizer.availableVoicesDidChangeNotification, object: nil,
            queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.log(.voicesChanged(installed: AVSpeechSynthesisVoice.speechVoices().count))
            }
        })
    }

    #if os(iOS)
    private func watchSession() {
        let nc = NotificationCenter.default
        let s = AVAudioSession.sharedInstance()
        observers.append(nc.addObserver(forName: AVAudioSession.interruptionNotification,
                                        object: s, queue: .main) { [weak self] note in
            let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            guard let type = raw.flatMap(AVAudioSession.InterruptionType.init) else { return }
            MainActor.assumeIsolated {
                self?.log(.session(type == .began ? .interruptionBegan : .interruptionEnded))
                if type == .began { self?.lost() }
            }
        })
        observers.append(nc.addObserver(forName: AVAudioSession.routeChangeNotification,
                                        object: s, queue: .main) { [weak self] note in
            let raw = note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt
            let gone = raw.flatMap(AVAudioSession.RouteChangeReason.init) == .oldDeviceUnavailable
            MainActor.assumeIsolated {
                self?.log(.session(.route(reason: raw ?? 0)))
                if gone { self?.lost() }
            }
        })
        observers.append(nc.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification,
                                        object: s, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.log(.session(.mediaReset))
                self?.lost()
            }
        })
    }
    #endif

    private func lost() {
        guard current != nil else { return }
        onInterrupt?()
    }
}

extension AVSpeechEngine: AVSpeechSynthesizerDelegate {
    /// Observed for diagnostics only.
    public nonisolated func speechSynthesizer(_ s: AVSpeechSynthesizer,
                                              didStart u: AVSpeechUtterance) {
        let which = ObjectIdentifier(u)
        Task { @MainActor in self.began(which) }
    }

    public nonisolated func speechSynthesizer(_ s: AVSpeechSynthesizer,
                                              didFinish u: AVSpeechUtterance) {
        let which = ObjectIdentifier(u)
        Task { @MainActor in self.ended(which, cancelled: false) }
    }

    public nonisolated func speechSynthesizer(_ s: AVSpeechSynthesizer,
                                              didCancel u: AVSpeechUtterance) {
        let which = ObjectIdentifier(u)
        Task { @MainActor in self.ended(which, cancelled: true) }
    }
}

/// An installed system voice a user may pick.
public struct VoiceOption: Identifiable, Equatable, Sendable {
    public var id: String
    public var name: String
    public var language: String
    /// 3 premium, 2 enhanced, 1 default.
    public var quality: Int

    public init(id: String, name: String, language: String, quality: Int) {
        self.id = id
        self.name = name
        self.language = language
        self.quality = quality
    }

    public var label: String {
        switch quality {
        case 3: "\(name) (Premium)"
        case 2: "\(name) (Enhanced)"
        default: name
        }
    }
}

/// The voices worth offering: installed English voices, never novelty voices (bells,
/// whispers, robots), never a Personal Voice, and never a Siri voice (not reliably
/// available to apps), best quality first.
@MainActor
public enum VoiceCatalog {
    /// Stark is a youthful, energetic teammate: these system voices come closest, in order.
    /// None is a character voice; Known Limitations in stark-voice-output.md says so.
    public nonisolated static let preferredNames = ["Evan", "Nathan", "Aaron", "Alex", "Tom",
                                                    "Reed", "Eddy", "Rocko"]
    /// Offered when chosen, never the default: they read as someone else entirely.
    nonisolated static let neverDefault: Set = ["Grandma", "Grandpa", "Fred", "Ralph",
                                                "Junior", "Albert"]

    /// The default among the offered voices: the best quality of the first preferred name
    /// installed (this region first), else the best offered voice that suits Stark.
    public nonisolated static func pickDefault(_ options: [VoiceOption],
                                               language: String) -> VoiceOption? {
        func better(_ a: VoiceOption, _ b: VoiceOption) -> Bool {
            let ka = (a.quality, a.language == language ? 1 : 0)
            let kb = (b.quality, b.language == language ? 1 : 0)
            return ka > kb
        }
        for name in preferredNames {
            if let v = options.filter({ $0.name == name }).sorted(by: better).first { return v }
        }
        return options.filter { !neverDefault.contains($0.name) }.sorted(by: better).first
    }

    public static func options(language: String = preferredLanguage) -> [VoiceOption] {
        let base = String(language.prefix(2))
        return AVSpeechSynthesisVoice.speechVoices()
            .filter { $0.language.hasPrefix(base) }
            .filter { !$0.voiceTraits.contains(.isNoveltyVoice)
                && !$0.voiceTraits.contains(.isPersonalVoice)
                && !$0.identifier.contains(".siri.") }
            .map { VoiceOption(id: $0.identifier, name: $0.name, language: $0.language,
                               quality: quality($0.quality)) }
            .sorted {
                // This region before others, better before worse, then by name.
                let a = ($0.language == language ? 0 : 1, -$0.quality, $0.name)
                let b = ($1.language == language ? 0 : 1, -$1.quality, $1.name)
                return a < b
            }
    }

    /// The user's choice when still installed, else the best offered voice.
    /// The voice Default Voice means on this device.
    public static var defaultOption: VoiceOption? {
        pickDefault(options(), language: preferredLanguage)
    }

    /// How a voice setting became a voice: the chosen voice, another voice the system gave
    /// for the chosen identifier, the default because none was chosen or the chosen one is
    /// gone, the language's system voice, or nothing at all.
    public enum How: String, Sendable { case requested, substituted, `default`, language, none }

    public static func voice(_ identifier: String?) -> AVSpeechSynthesisVoice? {
        resolve(identifier).0
    }

    public static func resolve(_ identifier: String?) -> (AVSpeechSynthesisVoice?, How) {
        if let id = identifier, let v = AVSpeechSynthesisVoice(identifier: id) {
            return (v, v.identifier == id ? .requested : .substituted)
        }
        if let best = pickDefault(options(), language: preferredLanguage) {
            let v = AVSpeechSynthesisVoice(identifier: best.id)
            return (v, v == nil ? .none : .default)
        }
        let v = AVSpeechSynthesisVoice(language: preferredLanguage)
        return (v, v == nil ? .none : .language)
    }

    public nonisolated static var preferredLanguage: String {
        let lang = AVSpeechSynthesisVoice.currentLanguageCode()
        return lang.hasPrefix("en") ? lang : "en-US"
    }

    static func quality(_ q: AVSpeechSynthesisVoiceQuality) -> Int {
        switch q {
        case .premium: 3
        case .enhanced: 2
        default: 1
        }
    }
}
