import Foundation

public protocol Cancellable: AnyObject {
    func cancel()
}

/// Schedules one callback after a delay; the app passes a Timer backed implementation.
public typealias Scheduler = @MainActor (TimeInterval, @escaping @MainActor () -> Void) -> Cancellable

/// Runs a StarkAnimationController with exactly one pending timer, fired at frame changes
/// only (3 to 12 Hz), never per display refresh. Every refresh cancels the pending timer
/// before scheduling the next, so state churn cannot stack loops.
@MainActor
public final class AnimationDriver {
    public let controller: StarkAnimationController
    public private(set) var frame: Int
    public var onFrame: (Int) -> Void = { _ in }
    private let clock: () -> TimeInterval
    private let schedule: Scheduler
    private var pending: Cancellable?
    private var paused = false

    public init(controller: StarkAnimationController, clock: @escaping () -> TimeInterval,
                schedule: @escaping Scheduler) {
        self.controller = controller
        self.clock = clock
        self.schedule = schedule
        frame = controller.frame(at: clock()).frame
    }

    /// Recompute after any controller input, and on every timer fire.
    public func refresh() {
        pending?.cancel()
        pending = nil
        let now = clock()
        let (f, next) = controller.frame(at: now)
        if f != frame {
            frame = f
            onFrame(f)
        }
        guard !paused, next.isFinite else { return }
        pending = schedule(max(next - now, 0.001)) { [weak self] in
            self?.pending = nil
            self?.refresh()
        }
    }

    /// Stop animating while nobody can see Stark (occluded panel); resume picks up in phase.
    public func setPaused(_ p: Bool) {
        guard p != paused else { return }
        paused = p
        if p {
            pending?.cancel()
            pending = nil
        } else {
            refresh()
        }
    }

    public var hasPendingTimer: Bool { pending != nil }

    deinit {
        MainActor.assumeIsolated { pending?.cancel() }
    }
}
