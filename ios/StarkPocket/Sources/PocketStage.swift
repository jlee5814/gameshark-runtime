import Foundation
import ImageIO
import Observation
import StarkKit

/// Stark's sprite atlas from the app bundle: the same manifest.json and stark-atlas.png
/// StarkMac ships, referenced from mac/StarkMac/Assets/stark, never copied or redrawn.
struct PocketAtlas {
    let manifest: SpriteManifest
    let frames: [CGImage]
    /// The same frames with the gym loadout baked in; nil when not bundled.
    var gymFrames: [CGImage]? = nil

    static func load(bundle: Bundle = .main) -> PocketAtlas? {
        guard let url = bundle.url(forResource: "manifest", withExtension: "json"),
              let m = try? SpriteManifest.load(from: url),
              let atlasURL = bundle.url(forResource: (m.atlas.file as NSString).deletingPathExtension,
                                        withExtension: (m.atlas.file as NSString).pathExtension),
              let src = CGImageSourceCreateWithURL(atlasURL as CFURL, nil),
              let atlas = CGImageSourceCreateImageAtIndex(src, 0, nil),
              atlas.width == m.atlas.width, atlas.height == m.atlas.height
        else { return nil }
        var frames: [CGImage] = []
        for f in m.frames {
            let r = CGRect(x: f.atlas[0], y: f.atlas[1], width: f.atlas[2], height: f.atlas[3])
            guard let img = atlas.cropping(to: r) else { return nil }
            frames.append(img)
        }
        let gym = bundle.url(forResource: "stark-atlas-gym", withExtension: "png")
            .flatMap { AtlasCrop.frames(at: $0, manifest: m) }
        return PocketAtlas(manifest: m, frames: frames, gymFrames: gym)
    }
}

private final class TimerToken: Cancellable {
    let timer: Timer
    init(_ t: Timer) { timer = t }
    func cancel() { timer.invalidate() }
}

@MainActor
private func mainScheduler(_ delay: TimeInterval, _ fire: @escaping @MainActor () -> Void) -> Cancellable {
    let t = Timer(timeInterval: delay, repeats: false) { _ in MainActor.assumeIsolated { fire() } }
    t.tolerance = min(0.004, delay * 0.1)
    RunLoop.main.add(t, forMode: .common)
    return TimerToken(t)
}

/// Follows runtime state with the same animation rules as StarkMac (StarkAnimation.base and
/// the working hold). Reads ClientState; writes nothing back.
@MainActor @Observable
final class PocketStage {
    let atlas: PocketAtlas
    private(set) var frame: Int
    private(set) var animation: StarkAnimation = .idle
    /// What Stark wears, from the runtime (ClientState.shownLoadout).
    private(set) var loadout: Loadout = .standard

    @ObservationIgnored private lazy var sheets = LoadoutFrames(standard: atlas.frames,
                                                                 gym: atlas.gymFrames)
    @ObservationIgnored private let controller: StarkAnimationController
    @ObservationIgnored private let driver: AnimationDriver
    @ObservationIgnored private var hold = WorkingHold()
    @ObservationIgnored private var holdTimer: Cancellable?
    @ObservationIgnored private var lastInputs: [AnyHashable] = []
    @ObservationIgnored private var lastActivity: TimeInterval
    @ObservationIgnored private var last: (ClientState, Bool, Int)?

    private static func now() -> TimeInterval { ProcessInfo.processInfo.systemUptime }

    init(atlas: PocketAtlas) {
        self.atlas = atlas
        let start = Self.now()
        controller = StarkAnimationController(manifest: atlas.manifest, now: start)
        driver = AnimationDriver(controller: controller, clock: Self.now, schedule: mainScheduler)
        frame = driver.frame
        lastActivity = start
        driver.onFrame = { [weak self] f in
            guard let self else { return }
            self.frame = f
            if self.animation != self.controller.current { self.animation = self.controller.current }
        }
        driver.refresh()
    }

    func follow(_ s: ClientState, listening: Bool, viewedSeq: Int) {
        last = (s, listening, viewedSeq)
        if loadout != s.shownLoadout { loadout = s.shownLoadout }
        let now = Self.now()
        let inputs: [AnyHashable] = [s.connection.isLive, s.isActiveSurface, s.isWorking,
                                     s.isThinking, listening, s.rows.count, viewedSeq]
        if inputs != lastInputs {
            lastInputs = inputs
            lastActivity = now
        }
        let held = hold.observe(working: s.isWorking, at: now)
        holdTimer?.cancel()
        holdTimer = nil
        if held && !s.isWorking {
            holdTimer = mainScheduler(max(hold.until - now, 0.001)) { [weak self] in
                guard let self, let l = self.last else { return }
                self.follow(l.0, listening: l.1, viewedSeq: l.2)
            }
        }
        controller.setBase(StarkAnimation.base(for: s, listening: listening, viewedSeq: viewedSeq,
                                               idleFor: now - lastActivity, workingHeld: held),
                           at: now)
        driver.refresh()
        frame = driver.frame
        if animation != controller.current { animation = controller.current }
    }

    /// Off screen (app in background): no timers at all.
    func setVisible(_ visible: Bool) { driver.setPaused(!visible) }

    var image: CGImage { sheets.frames(for: loadout)[frame] }
}
