import AppKit
import StarkKit

/// Builds the model, the panel, and the status item. The status item is the way back while
/// click through is on, since the panel then ignores every click.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var model: StarkModel!
    private var panel: PanelController!
    private var statusItem: NSStatusItem!

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
        if env["STARK_AVATAR_DEMO"] == "1" {
            model.stage?.setCycle(true)
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
            _ = model.stage?.animation
            _ = model.avatarScale
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
        let anim = model.stage.map { " anim=\($0.animation.rawValue) scale=\(model.avatarScale)" } ?? ""
        let line = "\(model.avatar.rawValue)\(anim) chat=\(model.chatOpen) rows=\(s.rows.count) "
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
        case "avatar" where args.count == 1:
            switch args[0] {
            case "follow": model.stage?.show(nil)
            case "cycle": model.stage?.setCycle(true)
            default: model.stage?.show(StarkAnimation(rawValue: args[0]))
            }
        case "scale" where args.count == 1:
            panel.setScale(Int(args[0]) ?? 0)
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
        statusItem.menu = menu
        panel.menuProvider = { [weak self] in self?.buildMenu() }
    }

    /// One menu for the status item and for right clicking Stark, rebuilt on every open
    /// so checkmarks and status are current.
    func buildMenu(into menu: NSMenu = NSMenu()) -> NSMenu {
        menu.removeAllItems()
        let s = model.state
        let gen = s.presence.map { " · \($0.surface) gen \($0.generation)" } ?? ""
        let status = NSMenuItem(title: "Stark \(s.identity.map { String($0.uuid.prefix(8)) } ?? "")"
            + " · \(connectionText(s.connection))\(gen)", action: nil, keyEquivalent: "")
        status.isEnabled = false
        menu.addItem(status)
        menu.addItem(.separator())
        menu.addItem(item("Show Chat", #selector(showChat), "k"))

        if model.stage != nil {
            let scale = NSMenu()
            for n in StarkModel.scales {
                let i = item("\(n)×", #selector(pickScale(_:)), "")
                i.tag = n
                i.state = model.avatarScale == n ? .on : .off
                scale.addItem(i)
            }
            menu.addItem(submenu("Avatar Scale", scale))

            let gallery = NSMenu()
            let follow = item("Follow Runtime", #selector(pickAnimation(_:)), "")
            follow.representedObject = nil
            follow.state = model.stage?.overriding == nil ? .on : .off
            gallery.addItem(follow)
            gallery.addItem(.separator())
            for a in StarkAnimation.allCases {
                let i = item(a.rawValue, #selector(pickAnimation(_:)), "")
                i.representedObject = a.rawValue
                i.state = model.stage?.overriding == a && model.stage?.cycling == false ? .on : .off
                gallery.addItem(i)
            }
            menu.addItem(submenu("Animation Gallery", gallery))
            let cycle = item("Cycle Demo", #selector(toggleCycle), "")
            cycle.state = model.stage?.cycling == true ? .on : .off
            menu.addItem(cycle)
        }

        let ct = item("Click Through", #selector(toggleClickThrough), "t")
        ct.state = model.clickThrough ? .on : .off
        menu.addItem(ct)
        menu.addItem(item("Reconnect", #selector(reconnect), "r"))
        menu.addItem(item("Save Snapshot", #selector(saveSnapshot), "s"))
        menu.addItem(.separator())
        menu.addItem(item("Quit StarkMac", #selector(quit), "q"))
        return menu
    }

    private func connectionText(_ c: Connection) -> String {
        switch c {
        case .live: model.state.isActiveSurface ? "live" : "read only"
        case .disconnected(let why): why ?? "offline"
        default: "connecting"
        }
    }

    private func submenu(_ title: String, _ sub: NSMenu) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        i.submenu = sub
        return i
    }

    private func item(_ title: String, _ action: Selector, _ key: String) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: action, keyEquivalent: key)
        i.target = self
        return i
    }

    func menuNeedsUpdate(_ menu: NSMenu) { _ = buildMenu(into: menu) }

    @objc private func pickScale(_ sender: NSMenuItem) { panel.setScale(sender.tag) }

    @objc private func pickAnimation(_ sender: NSMenuItem) {
        model.stage?.show((sender.representedObject as? String).flatMap(StarkAnimation.init(rawValue:)))
    }

    @objc private func toggleCycle() { model.stage?.setCycle(!(model.stage?.cycling ?? false)) }

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
