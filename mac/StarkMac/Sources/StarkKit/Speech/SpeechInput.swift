import Foundation

/// Whether this device can listen right now, and if not, why.
public enum InputAvailability: Equatable, Sendable {
    public enum Unavailable: String, Sendable {
        /// The app was built without the microphone and speech recognition usage strings.
        case notConfigured = "not_configured"
        /// No recognizer exists for English on this device.
        case noRecognizer = "no_recognizer"
        /// The recognizer exists but cannot run on device; Stark never falls back to a server.
        case noOnDevice = "no_on_device"
        /// The recognizer is temporarily unavailable.
        case offline
    }

    case ready
    /// Microphone or speech recognition access has not been asked yet.
    case needsPermission
    case denied
    case restricted
    case unavailable(Unavailable)

    public var name: String {
        switch self {
        case .ready: "ready"
        case .needsPermission: "needs_permission"
        case .denied: "denied"
        case .restricted: "restricted"
        case .unavailable(let u): "unavailable_" + u.rawValue
        }
    }

    /// What the user sees when push to talk is off; nil when it can be pressed.
    public var explanation: String? {
        switch self {
        case .ready, .needsPermission:
            nil
        case .denied:
            "Microphone or speech recognition access is off. Turn both on in Settings to talk to Stark."
        case .restricted:
            "Microphone or speech recognition is restricted on this device."
        case .unavailable(.notConfigured):
            "This build cannot use the microphone."
        case .unavailable(.noRecognizer):
            "English speech recognition is not available on this device."
        case .unavailable(.noOnDevice):
            "On device English recognition is not installed. Voice input stays off rather than "
                + "sending audio to a server."
        case .unavailable(.offline):
            "Speech recognition is unavailable right now."
        }
    }
}

/// What a recognizer reports for one capture session.
public enum InputEvent: Equatable, Sendable {
    case partial(String)
    case final(String)
    case failed(code: Int)
    /// The system took the microphone or the audio route away.
    case interrupted
}

/// Turns speech into text on this device. SFSpeechInput is the shipped conformer; tests use
/// a fake. An input never decides what is sent: it only reports text to CaptureCoordinator.
@MainActor
public protocol SpeechInput: AnyObject {
    var availability: InputAvailability { get }
    /// Called when availability may have changed on its own (the recognizer came back).
    var availabilityChanged: (@MainActor () -> Void)? { get set }
    /// Ask for microphone and speech recognition access; never starts listening.
    func requestPermission(_ done: @escaping @MainActor (InputAvailability) -> Void)
    /// Open the microphone and recognize on device. Every event carries `session`, so a
    /// callback from an older session can be told apart and dropped.
    func start(session: Int, _ events: @escaping @MainActor (Int, InputEvent) -> Void)
    /// Close the microphone and wait for the final transcript.
    func finish()
    /// Stop everything now and release the microphone; no event follows.
    func cancel()
}
