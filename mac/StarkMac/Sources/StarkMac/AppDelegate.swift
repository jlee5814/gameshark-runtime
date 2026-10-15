import AppKit
import StarkKit

/// Builds the model, the panel, and the status item. The status item is the way back while
/// click through is on, since the panel then ignores every click.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var model: StarkModel!
    private var panel: PanelController!
    private var statusItem: NSStatusItem!
    private var clickThroughItem: NSMenuItem!
    private var stateItem: NSMenuItem!

    func applicationDidFinishLaunching(_ notification: Notification) {
        model = StarkModel(config: .fromEnvironment())
        panel = PanelController(model: model)
        panel.show()
        buildStatusItem()
        model.start()
        let env = ProcessInfo.processInfo.environment
        if env["STARK_CLICK_THROUGH"] == "1" {
            setClickThrough(true)
        }
        if env["STARK_DEMO_CONTROL"] == "1" {
            listenForDemoControl()
        }
        if let dir = env["STARK_SNAPSHOT_DIR"] {
            snapshotDir = URL(fileURLWithPath: dir)
            watchForSnapshots()
        }
    }

    private var snapshotDir: URL?
    private var snapshotCount = 0
    private var lastShot = ""

    /// Demo aid: write a PNG and a line of state each time what Stark shows changes.
    private func watchForSnapshots() {
        withObservationTracking {
            _ = model.avatar
            _ = model.chatOpen
            _ = model.state.rows.count
            _ = model.state.presence
        } onChange: {
            DispatchQueue.main.async { [weak self] in
                MainActor.assumeIsolated {
                    self?.recordSnapshot()
                    self?.watchForSnapshots()
                }
            }
        }
    }

    private func recordSnapshot() {
        guard let dir = snapshotDir else { return }
        let s = model.state
        let line = "\(model.avatar.rawValue) chat=\(model.chatOpen) rows=\(s.rows.count) "
            + "presence=\(s.presence.map { "\($0.surface)@\($0.generation)" } ?? "-") "
            + "origin=\(Int(panel.panel.frame.minX)),\(Int(panel.panel.frame.minY))"
        guard line != lastShot else { return }
        lastShot = line
        snapshotCount += 1
        let name = String(format: "%03d-%@", snapshotCount, model.avatar.rawValue)
        panel.snapshot(to: dir.appendingPathComponent(name + ".png"))
        let log = dir.appendingPathComponent("states.log")
        let entry = Data("\(name) \(line)\n".utf8)
        if let h = try? FileHandle(forWritingTo: log) {
            h.seekToEndOfFile()
            h.write(entry)
            try? h.close()
        } else {
            try? entry.write(to: log)
        }
    }

    /// Opt in demo channel (see starkctl). Distributed notifications reach only processes of
    /// this user, and every command maps to an action the panel's own UI already offers.
    private func listenForDemoControl() {
        DistributedNotificationCenter.default().addObserver(
            forName: Notification.Name("dev.gameshark.starkmac.control"), object: nil,
            queue: .main
        ) { [weak self] note in
            let cmd = note.userInfo?["cmd"] as? String ?? ""
            let args = note.userInfo?["args"] as? [String] ?? []
            MainActor.assumeIsolated { self?.demo(cmd, args) }
        }
    }

    private func demo(_ cmd: String, _ args: [String]) {
        switch cmd {
        case "move" where args.count == 2:
            if let x = Double(args[0]), let y = Double(args[1]) {
                panel.move(to: NSPoint(x: x, y: y))
            }
        case "chat":
            panel.setChat(open: args.first != "close")
        case "say":
            panel.setChat(open: true)
            model.draft = args.joined(separator: " ")
            model.send()
        case "clickthrough":
            setClickThrough(args.first != "off")
        case "reconnect":
            model.client.reconnectNow()
        case "snapshot" where args.count == 1:
            panel.snapshot(to: URL(fileURLWithPath: args[0]))
        default:
            NSLog("starkctl: unknown command %@", cmd)
        }
        recordSnapshot()
    }

    @objc private func saveSnapshot() {
        let dir = snapshotDir ?? FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Desktop")
        let url = dir.appendingPathComponent("Stark-\(Int(Date().timeIntervalSince1970)).png")
        panel.snapshot(to: url)
    }

    func applicationWillTerminate(_ notification: Notification) {
        model.client.stop()
    }

    private func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        DistributedNotificationCenter.default().suspended = false
        statusItem.button?.image = NSImage(systemSymbolName: "circle.hexagongrid.fill",
                                           accessibilityDescription: "Stark")
        let menu = NSMenu()
        menu.delegate = self
        stateItem = NSMenuItem(title: "Stark", action: nil, keyEquivalent: "")
        stateItem.isEnabled = false
        menu.addItem(stateItem)
        menu.addItem(.separator())
        menu.addItem(item("Show Chat", #selector(showChat), "k"))
        clickThroughItem = item("Click Through", #selector(toggleClickThrough), "t")
        menu.addItem(clickThroughItem)
        menu.addItem(item("Reconnect", #selector(reconnect), "r"))
        menu.addItem(item("Save Snapshot", #selector(saveSnapshot), "s"))
        menu.addItem(.separator())
        menu.addItem(item("Quit StarkMac", #selector(quit), "q"))
        statusItem.menu = menu
    }

    private func item(_ title: String, _ action: Selector, _ key: String) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
        i.target = self
        return i
    }

    func menuWillOpen(_ menu: NSMenu) {
        let s = model.state
        let gen = s.presence.map { " · \($0.surface) gen \($0.generation)" } ?? ""
        stateItem.title = "Stark \(s.identity.map { String($0.uuid.prefix(8)) } ?? "")"
            + " · \(model.avatar.rawValue)\(gen)"
        clickThroughItem.state = model.clickThrough ? .on : .off
    }

    @objc private func showChat() {
        if model.clickThrough { setClickThrough(false) }
        panel.setChat(open: true)
        panel.show()
    }

    @objc private func toggleClickThrough() { setClickThrough(!model.clickThrough) }

    private func setClickThrough(_ on: Bool) { panel.setClickThrough(on) }

    @objc private func reconnect() { model.client.reconnectNow() }

    @objc private func quit() { NSApp.terminate(nil) }
}
