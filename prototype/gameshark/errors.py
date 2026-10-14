"""Typed errors for the runtime.

Each error marks a distinct control path the supervisor and tests reason about.
"""


class CrashInjected(Exception):
    """Raised at a labeled crash point to model abrupt process death.

    A test catches this, discards the supervisor, and builds a fresh one on the
    same database to model a restart. Nothing in memory carries over.
    """


class FencedError(Exception):
    """A write was attempted under a lease generation that is no longer current.

    The stale writer lost ownership; the commit is rejected and rolled back.
    """


class LeaseDenied(Exception):
    """Ownership could not be acquired because another holder owns a live lease."""


class AmbiguousEffect(Exception):
    """A remote probe could not confirm whether the effect landed.

    The runtime refuses to guess. The effect transitions to blocked and the
    owning responsibility escalates.
    """


class BlockedEffect(Exception):
    """A previously blocked effect was retried without explicit resolution."""


class StaleFenceRejected(Exception):
    """The destination rejected a write carrying a fence older than one it already accepted.

    This is the destination side guarantee that local fencing alone cannot provide: it
    stops a stale worker from producing an external effect after another owner took over.
    """
