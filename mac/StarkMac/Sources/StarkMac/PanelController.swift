import AppKit
import StarkKit
import SwiftUI

/// Owns the floating panel: size for the open or closed chat, drag, saved position, and
/// click through. Position is surface local UI state, kept in UserDefaults.
@MainActor
final class PanelController {
    static let inset: CGFloat = 8
    static let cardSize = CGSize(width: 340, height: 380)
    private static let originKey = "panelOrigin"

    let panel: FloatingPanel
    private let model: StarkModel
    private var dragStart: (mouse: NSPoint, origin: NSPoint)?
    private var lastMouseX: CGFloat = 0
    /// Builds the right click menu; the app delegate shares it with the status item.
    var menuProvider: () -> NSMenu? = { nil }

    init(model: StarkModel) {
        self.model = model
        panel = FloatingPanel(contentRect: NSRect(origin: .zero, size: NSSize(width: 10, height: 10)))
        let host = AvatarHostingView(rootView: CompanionView(model: model, controller: self))
        host.sizingOptions = []
        panel.contentView = host
        host.menuProvider = { [weak self] in self?.menuProvider() }
        model.backingScale = panel.backingScaleFactor
        panel.setFrame(NSRect(origin: Self.savedOrigin() ?? defaultOrigin(), size: size(open: false)),
                       display: false)
        clampToScreen()
        let center = NotificationCenter.default
        center.addObserver(forName: NSWindow.didChangeBackingPropertiesNotification, object: panel,
                           queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.backingChanged() }
        }
        center.addObserver(forName: NSWindow.didChangeOcclusionStateNotification, object: panel,
                           queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.model.stage?.setVisible(self.panel.occlusionState.contains(.visible))
            }
        }
    }

    func show() { panel.orderFrontRegardless() }

    func size(open: Bool) -> NSSize {
        let a = model.avatarSize
        let closed = NSSize(width: a.width + 2 * Self.inset, height: a.height + 2 * Self.inset)
        guard open else { return closed }
        return NSSize(width: max(closed.width, Self.cardSize.width + 2 * Self.inset),
                      height: closed.height + Self.cardSize.height + 6)
    }

    /// Resize for a new scale or display, keeping Stark's feet where they were.
    func relayout() {
        var f = panel.frame
        f.size = size(open: model.chatOpen)
        panel.setFrame(f, display: true)
        clampToScreen()
    }

    func setScale(_ s: Int) {
        guard StarkModel.scales.contains(s) else { return }
        model.avatarScale = s
        relayout()
    }

    private func backingChanged() {
        guard model.backingScale != panel.backingScaleFactor else { return }
        model.backingScale = panel.backingScaleFactor
        relayout()
    }

    // MARK: chat

    func toggleChat() {
        model.stage?.poke()
        setChat(open: !model.chatOpen)
    }

    func openChat() {
        model.stage?.poke()
        setChat(open: true)
        panel.makeKey()
    }

    func setChat(open: Bool) {
        guard model.chatOpen != open else { return }
        // Grow or shrink upward: the bottom left corner, where Stark sits, stays put.
        var frame = panel.frame
        frame.size = size(open: open)
        if open {
            model.chatOpen = true
            panel.setFrame(frame, display: true)
            clampToScreen()
            panel.makeKey()
        } else {
            model.chatOpen = false
            panel.setFrame(frame, display: true)
        }
    }

    // MARK: drag

    func dragChanged() {
        let mouse = NSEvent.mouseLocation
        if dragStart == nil {
            dragStart = (mouse, panel.frame.origin)
            lastMouseX = mouse.x
        }
        model.stage?.dragged(dx: mouse.x - lastMouseX)
        lastMouseX = mouse.x
        guard let s = dragStart else { return }
        panel.setFrameOrigin(NSPoint(x: s.origin.x + mouse.x - s.mouse.x,
                                     y: s.origin.y + mouse.y - s.mouse.y))
    }

    func dragEnded() {
        dragStart = nil
        model.stage?.dragEnded()
        settle()
    }

    /// Place the panel as a finished drag would.
    func move(to origin: NSPoint) {
        panel.setFrameOrigin(origin)
        settle()
    }

    private func settle() {
        clampToScreen()
        let o = panel.frame.origin
        UserDefaults.standard.set([o.x, o.y], forKey: Self.originKey)
    }

    // MARK: click through

    func setClickThrough(_ on: Bool) {
        model.clickThrough = on
        panel.ignoresMouseEvents = on
        if on { setChat(open: false) }
    }

    // MARK: snapshot

    /// Render the panel's own view to a PNG. Needs no screen recording permission, so the
    /// demo can capture evidence of each avatar state.
    @discardableResult
    func snapshot(to url: URL) -> Bool {
        guard let view = panel.contentView,
              let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return false }
        view.cacheDisplay(in: view.bounds, to: rep)
        guard let png = rep.representation(using: .png, properties: [:]) else { return false }
        return (try? png.write(to: url)) != nil
    }

    // MARK: placement

    private static func savedOrigin() -> NSPoint? {
        guard let xy = UserDefaults.standard.array(forKey: originKey) as? [Double], xy.count == 2
        else { return nil }
        return NSPoint(x: xy[0], y: xy[1])
    }

    private func defaultOrigin() -> NSPoint {
        let v = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let side = size(open: false)
        return NSPoint(x: v.maxX - side.width - 24, y: v.minY + 24)
    }

    /// Keep the panel fully on the screen it mostly occupies.
    private func clampToScreen() {
        let f = panel.frame
        let screen = NSScreen.screens.max {
            $0.visibleFrame.intersection(f).area < $1.visibleFrame.intersection(f).area
        } ?? NSScreen.main
        guard let v = screen?.visibleFrame else { return }
        var o = f.origin
        o.x = min(max(o.x, v.minX), v.maxX - f.width)
        o.y = min(max(o.y, v.minY), v.maxY - f.height)
        if o != f.origin { panel.setFrameOrigin(o) }
    }
}

private extension NSRect {
    var area: CGFloat { isNull ? 0 : width * height }
}

/// Hosting view whose right click shows the shared avatar menu.
final class AvatarHostingView: NSHostingView<CompanionView> {
    var menuProvider: () -> NSMenu? = { nil }

    override func menu(for event: NSEvent) -> NSMenu? { menuProvider() }
}
