import Foundation
import ImageIO
import Testing
@testable import StarkKit

let assets = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    .appendingPathComponent("Assets/stark")

func manifest() throws -> SpriteManifest {
    try SpriteManifest.load(from: assets.appendingPathComponent("manifest.json"))
}

/// Deterministic randomness: replays a fixed sequence.
final class Dice: @unchecked Sendable {
    var values: [Double]
    init(_ v: [Double]) { values = v }
    func next() -> Double { values.isEmpty ? 0.5 : values.removeFirst() }
}

@Suite struct ManifestTests {
    @Test func bundledManifestMatchesTheAtlas() throws {
        let m = try manifest()
        let src = try #require(CGImageSourceCreateWithURL(
            assets.appendingPathComponent(m.atlas.file) as CFURL, nil))
        let img = try #require(CGImageSourceCreateImageAtIndex(src, 0, nil))
        #expect(img.width == m.atlas.width && img.height == m.atlas.height)
        #expect(img.alphaInfo != .none)
        #expect(m.frames.count == 74)
        #expect(m.anchor.y < m.canvas.height && m.anchor.x < m.canvas.width)
    }

    @Test func everyAnimationNamesRealFrames() throws {
        let m = try manifest()
        let ids = Set(m.frames.map(\.id))
        for a in StarkAnimation.allCases {
            let clip = try #require(m.animations[a.rawValue])
            #expect(!clip.frames.isEmpty && clip.frames.allSatisfy(ids.contains))
        }
        #expect(m.animations["working"]?.fallback != nil)
        #expect(m.animations["error"]?.fallback != nil)
    }
}

@Suite struct ControllerTests {
    func controller(_ dice: [Double] = [0.5], now: TimeInterval = 0) throws -> StarkAnimationController {
        let d = Dice(dice)
        return StarkAnimationController(manifest: try manifest(), now: now, random: { d.next() })
    }

    func frames(_ name: String) throws -> [Int] {
        let m = try manifest()
        let idx = m.frameIndex
        return m.animations[name]!.frames.map { idx[$0]! }
    }

    @Test func idleLoopsAndBlinksInsideTheInterval() throws {
        // gap = 3.5 + 3.5 * 0.0 = 3.5 s; then 0.9 picks blink, not lookAround.
        let c = try controller([0.0, 0.9, 0.5])
        let idle = try frames("idle"), blink = try frames("blink")
        #expect(c.frame(at: 0).frame == idle[0])
        #expect(c.frame(at: 1.0).frame == idle[3])           // 3 fps
        #expect(c.frame(at: 1.4).frame == idle[0])           // looped
        #expect(c.frame(at: 3.49).next <= 3.5)               // wakes for the beat
        #expect(c.frame(at: 3.5).frame == blink[0])
        #expect(c.current == .blink)
        let end = 3.5 + Double(blink.count) / 12
        #expect(c.frame(at: end + 0.01).frame != blink.last!)
        #expect(c.current == .idle)
    }

    @Test func lookAroundSometimesReplacesTheBlink() throws {
        let c = try controller([0.0, 0.05])
        _ = c.frame(at: 3.6)
        #expect(c.current == .lookAround)
    }

    @Test func blinkGapStaysWithinBounds() throws {
        for r in [0.0, 0.25, 0.999] {
            let c = try controller([r, 0.9])
            let gap = StarkAnimationController.blinkEvery.lowerBound
                + (StarkAnimationController.blinkEvery.upperBound
                   - StarkAnimationController.blinkEvery.lowerBound) * r
            _ = c.frame(at: gap - 0.01)
            #expect(c.current == .idle)
            _ = c.frame(at: gap + 0.001)
            #expect(c.current == .blink)
        }
    }

    @Test func oneShotReturnsToTheCurrentBase() throws {
        let c = try controller([0.0, 0.9])
        _ = c.frame(at: 3.6)
        #expect(c.current == .blink)
        c.setBase(.thinking, at: 3.62)                      // base changes mid blink
        #expect(c.current == .thinking)
        let blink = try frames("blink")
        #expect(c.frame(at: 10).frame != blink.first!)
    }

    @Test func notifyPlaysItsIntroOnceThenLoopsTheWave() throws {
        let c = try controller()
        let f = try frames("notify")
        c.setBase(.notify, at: 0)
        let fps = 8.0
        let seen = (0..<30).map { c.frame(at: Double($0) / fps + 0.001).frame }
        #expect(Array(seen.prefix(f.count)) == f)
        let loop = Array(f[5...])
        #expect(Array(seen[f.count..<(f.count + loop.count)]) == loop)
        #expect(!seen.dropFirst(f.count).contains(f[0]))     // the hop never repeats
    }

    @Test func dragWalksTheRightWayAndStopsOnRelease() throws {
        let c = try controller()
        c.setBase(.listening, at: 0)
        c.setMotion(.left, at: 1)
        #expect(c.current == .walkLeft)
        let walk = try frames("walkLeft")
        #expect(c.frame(at: 1).frame == walk[0])
        c.setMotion(.right, at: 2)
        #expect(c.current == .walkRight)
        c.setMotion(nil, at: 3)
        #expect(c.current == .listening)
    }

    @Test func overrideWinsAndReleases() throws {
        let c = try controller()
        c.setBase(.thinking, at: 0)
        c.setMotion(.right, at: 0)
        c.setOverride(.sleep, at: 1)
        #expect(c.current == .sleep)
        c.setOverride(nil, at: 2)
        #expect(c.current == .walkRight)
    }

    @Test func everyAnimationProducesFramesForever() throws {
        let c = try controller()
        for a in StarkAnimation.allCases {
            c.setOverride(a, at: 0)
            var t = 0.0
            for _ in 0..<50 {
                let (_, next) = c.frame(at: t)
                #expect(next > t)
                if !next.isFinite { break }
                t = next
            }
        }
    }
}

@Suite struct RuntimeMappingTests {
    func state(live: Bool = true, surface: String = "mac", working: Bool = false,
               rows: [Row] = [], blocked: Bool = false) -> ClientState {
        var s = ClientState(surface: "mac")
        s.connection = live ? .live : .disconnected("gone")
        s.presence = PresenceInfo(surface: surface, home: "mac", generation: 1)
        s.working = working
        s.rows = rows
        if blocked {
            s.responsibilities = [Responsibility(id: "chat:stark:main", kind: "conversation",
                                                 state: "blocked", checkpoint: "Timeout")]
        }
        return s
    }

    let reply = [Row(seq: 1, role: "user", content: "hi"), Row(seq: 2, role: "assistant", content: "yo")]

    func base(_ s: ClientState, listening: Bool = false, viewed: Int = -1, idle: TimeInterval = 0) -> StarkAnimation {
        StarkAnimation.base(for: s, listening: listening, viewedSeq: viewed, idleFor: idle)
    }

    @Test func mapsOnlyExistingSignals() {
        #expect(base(state(live: false, working: true)) == .sleep)
        #expect(base(state(surface: "ghost", working: true)) == .idle)
        #expect(base(state(working: true, blocked: true)) == .error)
        #expect(base(state(working: true), listening: true) == .thinking)
        #expect(base(state(rows: reply), listening: true) == .listening)
        #expect(base(state(rows: reply), viewed: 1) == .notify)
        #expect(base(state(rows: reply), viewed: 2) == .idle)
        #expect(base(state(), idle: StarkAnimation.dozeAfter) == .sleep)
        #expect(base(state(), idle: StarkAnimation.dozeAfter - 1) == .idle)
    }

    @Test func workingIsNeverDerivedFromRuntimeState() {
        let all = [state(), state(working: true), state(live: false), state(blocked: true),
                   state(rows: reply), state(surface: "ghost")]
        for s in all {
            for l in [true, false] { #expect(base(s, listening: l) != .working) }
        }
    }
}

final class FakeTimer: Cancellable {
    var cancelled = false
    var fired = false
    let delay: TimeInterval
    let fire: @MainActor () -> Void
    init(_ d: TimeInterval, _ f: @escaping @MainActor () -> Void) { delay = d; fire = f }
    func cancel() { cancelled = true }
}

@MainActor @Suite struct DriverTests {
    @Test func churnNeverStacksTimers() throws {
        var now = 0.0
        var timers: [FakeTimer] = []
        let c = StarkAnimationController(manifest: try manifest(), now: 0)
        let d = AnimationDriver(controller: c, clock: { now }, schedule: { delay, f in
            let t = FakeTimer(delay, f)
            timers.append(t)
            return t
        })
        let states = StarkAnimation.allCases
        for i in 0..<1000 {
            c.setBase(states[i % states.count], at: now)
            if i % 7 == 0 { c.setMotion(i % 2 == 0 ? .left : .right, at: now) }
            if i % 11 == 0 { c.setMotion(nil, at: now) }
            d.refresh()
            now += 0.013
            #expect(timers.filter { !$0.cancelled && !$0.fired }.count <= 1)
        }
        // Fire the live timer a few hundred times: still one pending, frames advance.
        var seen = Set<Int>()
        for _ in 0..<300 {
            guard let live = timers.last(where: { !$0.cancelled && !$0.fired }) else { break }
            now += live.delay
            live.fired = true
            live.fire()
            seen.insert(d.frame)
            #expect(timers.filter { !$0.cancelled && !$0.fired }.count <= 1)
        }
        #expect(seen.count > 1)
    }

    @Test func pauseCancelsAndResumeReschedules() throws {
        var timers: [FakeTimer] = []
        let c = StarkAnimationController(manifest: try manifest(), now: 0)
        let d = AnimationDriver(controller: c, clock: { 0 }, schedule: { delay, f in
            let t = FakeTimer(delay, f); timers.append(t); return t
        })
        d.refresh()
        #expect(d.hasPendingTimer)
        d.setPaused(true)
        #expect(!d.hasPendingTimer && timers.allSatisfy(\.cancelled))
        d.setPaused(false)
        #expect(d.hasPendingTimer)
    }
}
