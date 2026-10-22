import Foundation

/// What Stark wears: presentation only. Derived from the user's own committed messages, so
/// it reads the same on every surface and survives a relaunch, and it never writes memory,
/// calls a tool, or asks the model anything.
public enum Outfit: String, Sendable {
    case standard
    /// Cap and AirPods, awake: the user said they are at the gym or working out.
    case gym

    /// Stark's gym still, beside manifest.json (mac/tools/extract_gym.py).
    public static let gymFile = "stark-gym.png"

    /// The newest user message that clearly says where they are decides; an exit outranks
    /// an arrival within one message, so "not working out" never reads as working out.
    public static func derive(_ rows: [Row]) -> Outfit {
        for row in rows.reversed() where row.role == "user" {
            let text = row.content.lowercased().replacingOccurrences(of: "\u{2019}", with: "'")
            if matches(leave, text) { return .standard }
            if matches(arrive, text) { return .gym }
        }
        return .standard
    }

    /// The resting poses the gym still stands in for. Work, thinking, errors, walking, and
    /// a dead link keep their own animations so status still reads.
    public static func covers(_ a: StarkAnimation) -> Bool {
        switch a {
        case .idle, .blink, .lookAround, .listening, .notify: true
        case .working, .thinking, .error, .sleep, .walkLeft, .walkRight: false
        }
    }

    private static let arrive = [
        #"\b(at|in) the gym\b"#,
        #"\b(heading|headed|going|off) to the gym\b"#,
        #"\bhitting the gym\b"#,
        #"\bworking out\b"#,
        #"\b(about|going|gonna|ready) to work ?out\b"#,
        #"\b(i'?m|i am) lifting\b"#,
        #"\blifting weights\b"#,
        #"\b(about|going|gonna|ready) to train\b"#,
        #"\bexercising\b"#,
        #"\b(doing|starting) (a|my) workout\b"#,
    ].map(regex)

    private static let leave = [
        #"\b(left|leaving|back from|home from|done at|out of) the gym\b"#,
        #"\b(done|finished|stopped) working out\b"#,
        #"\b(finished|done with|wrapped up) (my |the )?workout\b"#,
        #"\bnot (at|in|going to) the gym\b"#,
        #"\bskip(ping)? the gym\b"#,
        #"\b(not|no longer) (working out|exercising|lifting)\b"#,
    ].map(regex)

    private static func regex(_ p: String) -> NSRegularExpression {
        try! NSRegularExpression(pattern: p)        // literals above; a typo fails every test
    }

    private static func matches(_ patterns: [NSRegularExpression], _ text: String) -> Bool {
        let range = NSRange(text.startIndex..., in: text)
        return patterns.contains { $0.firstMatch(in: text, range: range) != nil }
    }
}
