import AppKit

/// A borderless, transparent panel that floats above normal windows on every Space without
/// taking activation from the frontmost app. Public AppKit only.
final class FloatingPanel: NSPanel {
    init(contentRect: NSRect) {
        super.init(contentRect: contentRect, styleMask: [.borderless, .nonactivatingPanel],
                   backing: .buffered, defer: false)
        isFloatingPanel = true
        level = .floating
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        backgroundColor = .clear
        isOpaque = false
        hasShadow = false
        hidesOnDeactivate = false
        becomesKeyOnlyIfNeeded = true
        isReleasedWhenClosed = false
        animationBehavior = .utilityWindow
    }

    // Key status lets the chat field take typing; the panel still never becomes main and
    // never activates the app (nonactivatingPanel), so the user's app keeps focus.
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}
