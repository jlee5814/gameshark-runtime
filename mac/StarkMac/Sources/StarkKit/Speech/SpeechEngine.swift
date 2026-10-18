import Foundation

/// One committed assistant row read aloud. The token tells a replay apart from the first
/// reading, so a late finish from a cancelled utterance never ends a newer one.
public struct Utterance: Equatable, Sendable {
    public struct ID: Hashable, Sendable {
        public var conversation: String
        public var seq: Int
        public var token: Int
    }

    public var id: ID
    /// The row's content exactly as the runtime committed it.
    public var text: String
    public var voice: VoiceSettings
}

/// Renders utterances as audio. AVSpeechEngine is the shipped conformer; a neural engine
/// can replace it without touching the surfaces. An engine never decides what to say.
@MainActor
public protocol SpeechEngine: AnyObject {
    /// Speak now, replacing anything playing.
    func speak(_ u: Utterance)
    func stop()
    /// The utterance played to its end or was cancelled.
    var onFinish: ((Utterance.ID) -> Void)? { get set }
    /// The system took the audio away (a call, Siri, the output device left).
    var onInterrupt: (() -> Void)? { get set }
}

/// How this device's Stark sounds: a local preference, never part of his persona and
/// never sent to the runtime.
public struct VoiceSettings: Codable, Equatable, Sendable {
    public var muted = false
    /// An installed system voice; nil picks VoiceCatalog's default.
    public var voiceIdentifier: String?
    /// A multiple of the engine's default speaking rate.
    public var rate = 1.05
    public var pitch = 1.05

    public static let rateRange = 0.85...1.2
    public static let pitchRange = 0.9...1.2

    public init() {}

    public func clamped() -> VoiceSettings {
        var v = self
        v.rate = min(max(rate, Self.rateRange.lowerBound), Self.rateRange.upperBound)
        v.pitch = min(max(pitch, Self.pitchRange.lowerBound), Self.pitchRange.upperBound)
        return v
    }
}

/// Where a device keeps its voice settings and handled watermarks.
public protocol SpeechStore: AnyObject {
    func integer(_ key: String) -> Int?
    func setInteger(_ value: Int, _ key: String)
    func data(_ key: String) -> Data?
    func setData(_ value: Data, _ key: String)
}

public final class DefaultsSpeechStore: SpeechStore {
    private let defaults: UserDefaults

    public init(_ defaults: UserDefaults = .standard) { self.defaults = defaults }

    public func integer(_ key: String) -> Int? { defaults.object(forKey: key) as? Int }
    public func setInteger(_ value: Int, _ key: String) { defaults.set(value, forKey: key) }
    public func data(_ key: String) -> Data? { defaults.data(forKey: key) }
    public func setData(_ value: Data, _ key: String) { defaults.set(value, forKey: key) }
}

/// A store that lives as long as the object; for tests and previews.
public final class MemorySpeechStore: SpeechStore {
    private var ints: [String: Int] = [:]
    private var blobs: [String: Data] = [:]

    public init() {}

    public func integer(_ key: String) -> Int? { ints[key] }
    public func setInteger(_ value: Int, _ key: String) { ints[key] = value }
    public func data(_ key: String) -> Data? { blobs[key] }
    public func setData(_ value: Data, _ key: String) { blobs[key] = value }
}

/// The highest seq this device has spoken or chosen not to speak, per agent, conversation,
/// and surface: a test runtime (another uuid) never moves the user's own Stark's mark.
public enum SpeechLedger {
    public static func key(agent: String, conversation: String, surface: String) -> String {
        "stark.voice.handled.\(agent).\(conversation).\(surface)"
    }

    static let settingsKey = "stark.voice.settings"
}
