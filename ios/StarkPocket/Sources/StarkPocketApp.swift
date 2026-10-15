import SwiftUI

@main
struct StarkPocketApp: App {
    @State private var model = PocketModel()
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            RootView(model: model)
                .onOpenURL { url in Task { await model.pair(url.absoluteString) } }
        }
        .onChange(of: phase) { _, p in model.scenePhase(p) }
    }
}
