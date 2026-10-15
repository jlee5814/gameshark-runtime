import AppKit

// An accessory app: no Dock icon, no main menu. The status item is the only chrome.
let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
