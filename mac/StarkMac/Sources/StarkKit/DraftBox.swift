import Foundation

/// A surface's unsent composer text, readable from the gateway queue when a jack asks for
/// it (GatewayClient.setDraftProvider). The UI writes it on the main actor.
public final class DraftBox: @unchecked Sendable {
    private let lock = NSLock()
    private var value = ""

    public init() {}

    public func set(_ v: String) { lock.withLock { value = v } }
    public func get() -> String? { lock.withLock { value.isEmpty ? nil : value } }
}
