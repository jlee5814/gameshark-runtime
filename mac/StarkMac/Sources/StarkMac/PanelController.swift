import AppKit
import StarkKit
import SwiftUI

/// Owns the floating panel: size for the open or closed chat, drag, saved position, and
/// click through. Position is surface local UI state, kept in UserDefaults.
@MainActor
final class PanelController {
    static let avatarSide: CGFloat = 112
    static let inset: CGFloat = 8
    static let cardSize = CGSize(width: 340, height: 380)
    private static let originKey = "panelOrigin"

    let panel: FloatingPanel
    private let model: StarkModel
    private var dragStart: (mouse: NSPoint, origin: NSPoint)?

    init(model: StarkModel) {
        self.model = model
        panel = FloatingPanel(contentRect: NSRect(origin: .zero, size: Self.size(open: false)))
        let host = NSHostingView(rootView: CompanionView(model: model, controller: self))
        host.sizingOptions = []
        panel.contentView = host
        panel.setFrameOrigin(Self.savedOrigin() ?? Self.defaultOrigin())
        clampToScreen()
    }

    func show() { panel.orderFrontRegardless() }

    static func size(open: Bool) -> NSSize {
        let side = avatarSide + 2 * inset
        guard open else { return NSSize(width: side, height: side) }
        return NSSize(width: max(side, cardSize.width + 2 * inset),
                      height: side + cardSize.height + 6)
    }

    // MARK: chat

    func toggleChat() { setChat(open: !model.chatOpen) }

    func setChat(open: Bool) {
        guard model.chatOpen != open else { return }
        // Grow or shrink upward: the bottom left corner, where Stark sits, stays put.
        var frame = panel.frame
        let size = Self.size(open: open)
        frame.size = size
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
        if dragStart == nil { dragStart = (mouse, panel.frame.origin) }
        guard let s = dragStart else { return }
        panel.setFrameOrigin(NSPoint(x: s.origin.x + mouse.x - s.mouse.x,
                                     y: s.origin.y + mouse.y - s.mouse.y))
    }

    func dragEnded() {
        dragStart = nil
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

    private static func defaultOrigin() -> NSPoint {
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
