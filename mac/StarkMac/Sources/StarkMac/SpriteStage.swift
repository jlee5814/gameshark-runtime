import AppKit
import ImageIO
import Observation
import StarkKit

/// The decoded atlas: one CGImage per frame, cropped once. Crops share the atlas backing
/// store, so 74 frames cost one image in memory.
struct SpriteAtlas {
    let manifest: SpriteManifest
    let frames: [CGImage]
    /// The same frames with the gym loadout baked in; nil when not shipped.
    var gymFrames: [CGImage]? = nil

    /// Contents/Resources/stark in the app, else STARK_SPRITES, else the package's Assets
    /// directory (swift run during development).
    static func load() -> SpriteAtlas? {
        let candidates = [
            Bundle.main.resourceURL?.appendingPathComponent("stark"),
            ProcessInfo.processInfo.environment["STARK_SPRITES"].map { URL(fileURLWithPath: $0) },
            URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
                .deletingLastPathComponent().appendingPathComponent("Assets/stark"),
        ].compactMap { $0 }
        for dir in candidates {
            let manifestURL = dir.appendingPathComponent("manifest.json")
            guard FileManager.default.fileExists(atPath: manifestURL.path) else { continue }
            do {
                return try load(manifest: manifestURL, dir: dir)
            } catch {
                NSLog("StarkMac: sprites at %@ unusable: %@", dir.path, "\(error)")
            }
        }
        NSLog("StarkMac: no sprite atlas found; drawing the vector avatar")
        return nil
    }

    static func load(manifest url: URL, dir: URL) throws -> SpriteAtlas {
        let m = try SpriteManifest.load(from: url)
        let atlasURL = dir.appendingPathComponent(m.atlas.file)
        guard let src = CGImageSourceCreateWithURL(atlasURL as CFURL, nil),
              let atlas = CGImageSourceCreateImageAtIndex(src, 0, nil),
              atlas.width == m.atlas.width, atlas.height == m.atlas.height
        else { throw CocoaError(.fileReadCorruptFile, userInfo: [NSFilePathErrorKey: atlasURL.path]) }
        let frames = try m.frames.map { f -> CGImage in
            let r = CGRect(x: f.atlas[0], y: f.atlas[1], width: f.atlas[2], height: f.atlas[3])
            guard let img = atlas.cropping(to: r) else { throw CocoaError(.fileReadCorruptFile) }
            return img
        }
        let gym = AtlasCrop.frames(at: dir.appendingPathComponent(Loadout.gym.atlasFile),
                                   manifest: m)
        if gym == nil { NSLog("StarkMac: no gym atlas in %@; the gym loadout draws standard", dir.path) }
        return SpriteAtlas(manifest: m, frames: frames, gymFrames: gym)
    }
}

private final class TimerToken: Cancellable {
    let timer: Timer
    init(_ t: Timer) { timer = t }
    func cancel() { timer.invalidate() }
}

/// Main run loop timer in common modes, so Stark keeps animating while he is dragged.
@MainActor
func runLoopScheduler(_ delay: TimeInterval, _ fire: @escaping @MainActor () -> Void) -> Cancellable {
    let t = Timer(timeInterval: delay, repeats: false) { _ in MainActor.assumeIsolated { fire() } }
    t.tolerance = min(0.004, delay * 0.1)
    RunLoop.main.add(t, forMode: .common)
    return TimerToken(t)
}

/// Owns Stark's animation on screen: follows runtime state, walks while dragged, and
/// offers the gallery and demo cycle. Reads ClientState; writes nothing back.
@MainActor @Observable
final class SpriteStage {
    let atlas: SpriteAtlas
    private(set) var frame: Int
    private(set) var animation: StarkAnimation = .idle
    private(set) var cycling = false
    /// What Stark wears, from the runtime (ClientState.shownLoadout), or the gallery's pick.
    private(set) var loadout: Loadout = .standard
    /// Gallery: preview a loadout regardless of the runtime, until released with nil.
    private(set) var previewLoadout: Loadout?
    @ObservationIgnored private lazy var sheets = LoadoutFrames(standard: atlas.frames,
                                                                 gym: atlas.gymFrames)

    @ObservationIgnored private let controller: StarkAnimationController
    @ObservationIgnored private let driver: AnimationDriver
    @ObservationIgnored private var lastInputs: [AnyHashable] = []
    @ObservationIgnored private var lastActivity: TimeInterval
    @ObservationIgnored private var doze: Cancellable?
    @ObservationIgnored private var cycleTimer: Cancellable?
    @ObservationIgnored private var follow: (ClientState, Bool, Int)?
    @ObservationIgnored private var hold = WorkingHold()
    @ObservationIgnored private var holdTimer: Cancellable?

    static let cycleOrder: [StarkAnimation] = [.idle, .blink, .listening, .thinking, .working,
                                               .notify, .walkRight, .walkLeft, .sleep, .error]
    static let cycleStep: TimeInterval = 3

    private static func now() -> TimeInterval { ProcessInfo.processInfo.systemUptime }

    init(atlas: SpriteAtlas) {
        self.atlas = atlas
        let start = Self.now()
        controller = StarkAnimationController(manifest: atlas.manifest, now: start)
        driver = AnimationDriver(controller: controller, clock: Self.now,
                                 schedule: runLoopScheduler)
        frame = driver.frame
        lastActivity = start
        driver.onFrame = { [weak self] f in self?.frame = f; self?.syncName() }
        driver.refresh()
    }

    /// Recompute the base animation from runtime state. Call on every ClientState change
    /// and when the chat field gains or loses focus.
    func follow(_ s: ClientState, listening: Bool, viewedSeq: Int) {
        follow = (s, listening, viewedSeq)
        let shown = previewLoadout ?? s.shownLoadout
        if loadout != shown { loadout = shown }
        let inputs: [AnyHashable] = [s.connection.isLive, s.isActiveSurface, s.working,
                                     s.isWorking, s.isThinking, listening, s.rows.count,
                                     viewedSeq, s.responsibilities.map(\.state)]
        let now = Self.now()
        if inputs != lastInputs {
            lastInputs = inputs
            lastActivity = now
            scheduleDoze()
        }
        let held = hold.observe(working: s.isWorking, at: now)
        holdTimer?.cancel()
        holdTimer = nil
        if held && !s.isWorking {
            // Re-follow once the hold ends, so Stark returns to thinking or idle on time.
            holdTimer = runLoopScheduler(max(hold.until - now, 0.001)) { [weak self] in
                guard let self, let f = self.follow else { return }
                self.follow(f.0, listening: f.1, viewedSeq: f.2)
            }
        }
        let base = StarkAnimation.base(for: s, listening: listening, viewedSeq: viewedSeq,
                                       idleFor: now - lastActivity, workingHeld: held)
        controller.setBase(base, at: now)
        update()
    }

    /// Any direct interaction wakes Stark up.
    func poke() {
        lastActivity = Self.now()
        scheduleDoze()
        if let f = follow { follow(f.0, listening: f.1, viewedSeq: f.2) }
    }

    func dragged(dx: CGFloat) {
        guard abs(dx) >= 0.5 else { return }
        controller.setMotion(dx < 0 ? .left : .right, at: Self.now())
        update()
    }

    func dragEnded() {
        controller.setMotion(nil, at: Self.now())
        poke()
        update()
    }

    /// Gallery: show one animation until released with nil.
    func show(_ a: StarkAnimation?) {
        setCycle(false)
        controller.setOverride(a, at: Self.now())
        update()
    }

    var overriding: StarkAnimation? { controller.overriding }

    /// Gallery: draw a loadout without asking the runtime; nil follows the runtime again.
    func preview(_ l: Loadout?) {
        previewLoadout = l
        if let f = follow { follow(f.0, listening: f.1, viewedSeq: f.2) } else { loadout = l ?? .standard }
    }

    var hasGymAtlas: Bool { sheets.gym != nil }

    func setCycle(_ on: Bool) {
        cycleTimer?.cancel()
        cycleTimer = nil
        cycling = on
        guard on else {
            controller.setOverride(nil, at: Self.now())
            update()
            return
        }
        step(0)
    }

    private func step(_ i: Int) {
        controller.setOverride(Self.cycleOrder[i % Self.cycleOrder.count], at: Self.now())
        update()
        cycleTimer = runLoopScheduler(Self.cycleStep) { [weak self] in self?.step(i + 1) }
    }

    func setVisible(_ visible: Bool) { driver.setPaused(!visible) }

    private func update() {
        driver.refresh()
        frame = driver.frame
        syncName()
    }

    private func syncName() {
        if animation != controller.current { animation = controller.current }
    }

    private func scheduleDoze() {
        doze?.cancel()
        doze = runLoopScheduler(StarkAnimation.dozeAfter + 0.5) { [weak self] in
            guard let self, let f = self.follow else { return }
            self.follow(f.0, listening: f.1, viewedSeq: f.2)
        }
    }

    var image: CGImage { sheets.frames(for: loadout)[frame] }

    /// Point size for an integer number of device pixels per sheet pixel.
    func size(scale: Int, backing: CGFloat) -> CGSize {
        let c = atlas.manifest.canvas
        return CGSize(width: CGFloat(c.width * scale) / backing,
                      height: CGFloat(c.height * scale) / backing)
    }
}
