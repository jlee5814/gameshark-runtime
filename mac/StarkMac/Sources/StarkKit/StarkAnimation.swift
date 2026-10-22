import Foundation

/// Stark's named animations; raw values are the manifest's clip names.
public enum StarkAnimation: String, CaseIterable, Sendable {
    case idle, blink, lookAround, listening, thinking, working, notify
    case walkLeft, walkRight, sleep, error

    /// Long quiet before Stark dozes off while connected.
    public static let dozeAfter: TimeInterval = 600

    /// The animation runtime state calls for. A pure read of ClientState: animation is view
    /// state and never writes identity, presence, duties, or inference.
    ///
    /// Working outranks thinking: a poll is short and a reply is long, so the reverse order
    /// would hide every poll behind a reply. workingHeld keeps a brief poll on screen for
    /// WorkingHold.minimum (view state only).
    ///
    /// At the gym Stark never dozes: the resting pose stays awake (Outfit.covers draws it).
    public static func base(for s: ClientState, listening: Bool, viewedSeq: Int,
                            idleFor: TimeInterval, workingHeld: Bool = false,
                            outfit: Outfit = .standard) -> StarkAnimation {
        if !s.connection.isLive { return .sleep }
        if !s.isActiveSurface { return .idle }          // ghost: drawn dimmed by the view
        if s.responsibilities.contains(where: { $0.state == "blocked" }) { return .error }
        if s.isWorking || workingHeld { return .working }
        // A reply still on its way (queued, or waiting for a model that is down) is
        // thinking even while no call runs; a failed one shows as its blocked duty.
        if s.isThinking || s.reply?.pending == true { return .thinking }
        if listening { return .listening }
        let newest = s.rows.last { $0.role == "assistant" }?.seq ?? -1
        if newest > viewedSeq { return .notify }
        if idleFor >= dozeAfter && outfit != .gym { return .sleep }
        return .idle
    }
}

/// Minimum dwell for the working animation. A GitHub poll may take 50 ms; without a hold
/// it would flash for a single frame. The hold counts from when work began, so long work
/// ends on time. View state only.
public struct WorkingHold: Sendable {
    public static let minimum: TimeInterval = 1.2
    public private(set) var until: TimeInterval = -.infinity
    private var active = false

    public init() {}

    /// Feed the runtime's working flag; returns whether working should still show.
    public mutating func observe(working: Bool, at now: TimeInterval) -> Bool {
        if working {
            if !active { until = now + Self.minimum }
            active = true
            return true
        }
        active = false
        return now < until
    }
}

/// Picks the frame to show at any moment. Precedence: a gallery override, then movement
/// (walking while dragged), then a one shot (blink, look around), then the base animation
/// set by runtime state. One shots return to whatever the base is when they finish.
///
/// Not thread safe; the app drives it from the main actor. Time and randomness are
/// injected so every transition is testable.
public final class StarkAnimationController {
    public enum Direction: Sendable { case left, right }

    struct Clip {
        let frames: [Int]
        let duration: TimeInterval
        let loop: Bool
        let loopFrom: Int
    }

    private struct Playing {
        let animation: StarkAnimation
        let start: TimeInterval
    }

    public static let blinkEvery: ClosedRange<TimeInterval> = 3.5...7.0
    public static let lookAroundChance = 0.15

    private let clips: [StarkAnimation: Clip]
    private let random: () -> Double
    public private(set) var base: StarkAnimation = .idle
    private var baseStart: TimeInterval
    private var oneShot: Playing?
    private var motion: Playing?
    private var override: Playing?
    private var nextIdleBeat: TimeInterval

    public init(manifest: SpriteManifest, now: TimeInterval,
                random: @escaping () -> Double = { Double.random(in: 0..<1) }) {
        let index = manifest.frameIndex
        var clips: [StarkAnimation: Clip] = [:]
        for a in StarkAnimation.allCases {
            let c = manifest.animations[a.rawValue]!            // validate() guarantees it
            clips[a] = Clip(frames: c.frames.compactMap { index[$0] }, duration: 1 / c.fps,
                            loop: c.loop, loopFrom: c.loopFrom ?? 0)
        }
        self.clips = clips
        self.random = random
        baseStart = now
        nextIdleBeat = now
        nextIdleBeat = now + gap()
    }

    /// What plays now, before any time passes.
    public var current: StarkAnimation {
        override?.animation ?? motion?.animation ?? oneShot?.animation ?? base
    }

    public func setBase(_ a: StarkAnimation, at now: TimeInterval) {
        guard a != base else { return }
        base = a
        baseStart = now
        if a != .idle { oneShot = nil }
        nextIdleBeat = now + gap()
    }

    public func setMotion(_ d: Direction?, at now: TimeInterval) {
        let a: StarkAnimation? = d.map { $0 == .left ? .walkLeft : .walkRight }
        guard a != motion?.animation else { return }
        motion = a.map { Playing(animation: $0, start: now) }
    }

    /// Gallery and demo: force an animation, or nil to follow runtime state again.
    public func setOverride(_ a: StarkAnimation?, at now: TimeInterval) {
        guard a != override?.animation else { return }
        override = a.map { Playing(animation: $0, start: now) }
    }

    public var overriding: StarkAnimation? { override?.animation }

    public func playOnce(_ a: StarkAnimation, at now: TimeInterval) {
        oneShot = Playing(animation: a, start: now)
    }

    /// The frame index (into the manifest's frames) to draw at now, and the time of the
    /// next change. Advances one shots and the idle beat as time passes.
    public func frame(at now: TimeInterval) -> (frame: Int, next: TimeInterval) {
        if let o = override { return sample(o.animation, o.start, now) }
        if let m = motion { return sample(m.animation, m.start, now) }
        if let s = oneShot {
            let clip = clips[s.animation]!
            let end = s.start + clip.duration * Double(clip.frames.count)
            if now < end { return sample(s.animation, s.start, now) }
            oneShot = nil
            nextIdleBeat = end + gap()
        }
        if base == .idle && now >= nextIdleBeat {
            let beat: StarkAnimation = random() < Self.lookAroundChance ? .lookAround : .blink
            oneShot = Playing(animation: beat, start: now)
            return sample(beat, now, now)
        }
        var out = sample(base, baseStart, now)
        if base == .idle { out.next = min(out.next, nextIdleBeat) }
        return out
    }

    private func gap() -> TimeInterval {
        let r = Self.blinkEvery
        return r.lowerBound + (r.upperBound - r.lowerBound) * random()
    }

    private func sample(_ a: StarkAnimation, _ start: TimeInterval, _ now: TimeInterval)
        -> (frame: Int, next: TimeInterval)
    {
        let clip = clips[a]!
        let n = clip.frames.count
        // The epsilon keeps an exact boundary on the new frame: floor((k * d) / d) can land
        // on k minus one, which would ask for a timer at the current instant.
        let step = max(0, Int(((now - start) / clip.duration + 1e-9).rounded(.down)))
        let i: Int
        if step < n {
            i = step
        } else if clip.loop {
            let span = n - clip.loopFrom
            i = clip.loopFrom + (step - n) % span
        } else {
            return (clip.frames[n - 1], .infinity)          // hold the last frame
        }
        return (clip.frames[i], start + Double(step + 1) * clip.duration)
    }
}
