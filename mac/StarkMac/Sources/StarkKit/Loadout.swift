import Foundation

/// What Stark wears (Phase 4.4B). The runtime owns it: a surface renders the snapshot's
/// value and loadout pushes, and asks for a change only through loadout_set. Presentation
/// only: no speech, audio route, memory, or inference path reads it, and avatar AirPods
/// say nothing about the user's Bluetooth route.
public enum Loadout: String, Codable, Equatable, Sendable, CaseIterable {
    case standard = "default"
    case gym

    /// An unknown value from a newer runtime draws the standard sprite.
    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = Loadout(rawValue: raw) ?? .standard
    }

    /// The atlas file a renderer draws for this loadout, beside manifest.json.
    public var atlasFile: String {
        switch self {
        case .standard: "stark-atlas.png"
        case .gym: "stark-atlas-gym.png"
        }
    }
}

/// The runtime's loadout row: what shows, what the newest explicit message said, and the
/// manual pin, if any. A pin wins over inferred context until cleared (auto).
public struct LoadoutState: Codable, Equatable, Sendable {
    public var effective: Loadout
    public var inferred: Loadout
    /// The manual pin; nil is auto. Wire name: override.
    public var pinned: Loadout?
    public var inferredSeq: Int?

    enum CodingKeys: String, CodingKey {
        case effective, inferred, inferredSeq
        case pinned = "override"
    }

    public static let standard = LoadoutState(effective: .standard, inferred: .standard,
                                              pinned: nil, inferredSeq: nil)

    public init(effective: Loadout, inferred: Loadout, pinned: Loadout?,
                inferredSeq: Int? = nil) {
        self.effective = effective
        self.inferred = inferred
        self.pinned = pinned
        self.inferredSeq = inferredSeq
    }
}

/// A manual override waiting for the runtime's answer. One at a time: a newer choice
/// replaces an unsent one, so only the user's last tap is ever applied.
public struct LoadoutOp: Equatable, Sendable {
    public var id: String
    /// nil clears the pin (auto).
    public var pin: Loadout?
}

/// A composer line that sets the loadout instead of chatting. Never sent as a row.
public enum LoadoutCommand: Equatable, Sendable {
    case pin(Loadout)
    case auto

    static let commands: [String: LoadoutCommand] = [
        "/gym": .pin(.gym), "/default": .pin(.standard), "/auto": .auto,
    ]

    /// nil for an ordinary message, which goes to the conversation unchanged.
    public static func parse(_ line: String) -> LoadoutCommand? {
        commands[line.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()]
    }

    /// The pin this asks for; nil means auto.
    public var requested: Loadout? {
        switch self {
        case .pin(let l): l
        case .auto: nil
        }
    }
}

struct LoadoutSetResult: Decodable {
    var loadout: LoadoutState
    var replayed: Bool
}

public enum LoadoutText {
    public static func name(_ l: Loadout) -> String {
        switch l {
        case .standard: "Default"
        case .gym: "Gym"
        }
    }
}
