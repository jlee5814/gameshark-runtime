import AVFoundation
import Foundation

/// The on device engine: AVSpeechSynthesizer, offline, no audio leaves the device.
///
/// iOS: a `.playback` / `.spokenAudio` session ducks other audio while Stark talks and is
/// released after. An interruption, a lost output route, or a media server reset ends the
/// utterance through onInterrupt; nothing resumes on its own.
@MainActor
public final class AVSpeechEngine: NSObject, SpeechEngine {
    public var onFinish: ((Utterance.ID) -> Void)?
    public var onInterrupt: (() -> Void)?

    private let synth = AVSpeechSynthesizer()
    private var current: (ObjectIdentifier, Utterance.ID)?
    private var observers: [NSObjectProtocol] = []

    public override init() {
        super.init()
        synth.delegate = self
        #if os(iOS)
        synth.usesApplicationAudioSession = true
        watchSession()
        #endif
    }

    public func speak(_ u: Utterance) {
        if synth.isSpeaking || synth.isPaused { synth.stopSpeaking(at: .immediate) }
        let v = u.voice.clamped()
        let say = AVSpeechUtterance(string: u.text)
        say.voice = VoiceCatalog.voice(v.voiceIdentifier)
        say.rate = Float(min(max(Double(AVSpeechUtteranceDefaultSpeechRate) * v.rate,
                                 Double(AVSpeechUtteranceMinimumSpeechRate)),
                             Double(AVSpeechUtteranceMaximumSpeechRate)))
        say.pitchMultiplier = Float(v.pitch)
        current = (ObjectIdentifier(say), u.id)
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

    private func ended(_ which: ObjectIdentifier) {
        guard let (obj, id) = current, obj == which else { return }
        current = nil
        deactivateSession()
        onFinish?(id)
    }

    private func activateSession() {
        #if os(iOS)
        let s = AVAudioSession.sharedInstance()
        try? s.setCategory(.playback, mode: .spokenAudio, options: [.duckOthers])
        try? s.setActive(true)
        #endif
    }

    private func deactivateSession() {
        #if os(iOS)
        guard !synth.isSpeaking else { return }
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        #endif
    }

    #if os(iOS)
    private func watchSession() {
        let nc = NotificationCenter.default
        let s = AVAudioSession.sharedInstance()
        observers.append(nc.addObserver(forName: AVAudioSession.interruptionNotification,
                                        object: s, queue: .main) { [weak self] note in
            let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            guard raw.flatMap(AVAudioSession.InterruptionType.init) == .began else { return }
            MainActor.assumeIsolated { self?.lost() }
        })
        observers.append(nc.addObserver(forName: AVAudioSession.routeChangeNotification,
                                        object: s, queue: .main) { [weak self] note in
            let raw = note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt
            guard raw.flatMap(AVAudioSession.RouteChangeReason.init) == .oldDeviceUnavailable
            else { return }
            MainActor.assumeIsolated { self?.lost() }
        })
        observers.append(nc.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification,
                                        object: s, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.lost() }
        })
    }
    #endif

    private func lost() {
        guard current != nil else { return }
        onInterrupt?()
    }
}

extension AVSpeechEngine: AVSpeechSynthesizerDelegate {
    public nonisolated func speechSynthesizer(_ s: AVSpeechSynthesizer,
                                              didFinish u: AVSpeechUtterance) {
        let which = ObjectIdentifier(u)
        Task { @MainActor in self.ended(which) }
    }

    public nonisolated func speechSynthesizer(_ s: AVSpeechSynthesizer,
                                              didCancel u: AVSpeechUtterance) {
        let which = ObjectIdentifier(u)
        Task { @MainActor in self.ended(which) }
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

    public static func voice(_ identifier: String?) -> AVSpeechSynthesisVoice? {
        if let id = identifier, let v = AVSpeechSynthesisVoice(identifier: id) { return v }
        if let best = pickDefault(options(), language: preferredLanguage) {
            return AVSpeechSynthesisVoice(identifier: best.id)
        }
        return AVSpeechSynthesisVoice(language: preferredLanguage)
    }

    public nonisolated static var preferredLanguage: String {
        let lang = AVSpeechSynthesisVoice.currentLanguageCode()
        return lang.hasPrefix("en") ? lang : "en-US"
    }

    private static func quality(_ q: AVSpeechSynthesisVoiceQuality) -> Int {
        switch q {
        case .premium: 3
        case .enhanced: 2
        default: 1
        }
    }
}
