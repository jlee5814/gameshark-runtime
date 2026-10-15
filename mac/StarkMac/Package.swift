// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "StarkMac",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "StarkMac", targets: ["StarkMac"]),
        .library(name: "StarkKit", targets: ["StarkKit"]),
        .executable(name: "starkctl", targets: ["starkctl"]),
    ],
    targets: [
        // Protocol, auth, transport, and the reconnecting client. No AppKit, so it tests headless.
        .target(name: "StarkKit"),
        // The floating companion: NSPanel, status item, SwiftUI avatar and chat.
        .executableTarget(name: "StarkMac", dependencies: ["StarkKit"]),
        // Demo driver: posts control notifications to an app launched with STARK_DEMO_CONTROL=1.
        .executableTarget(name: "starkctl"),
        .testTarget(name: "StarkKitTests", dependencies: ["StarkKit"]),
    ]
)
