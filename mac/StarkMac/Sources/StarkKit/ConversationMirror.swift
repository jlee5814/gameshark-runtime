import Foundation

/// Render cache of the companion conversation, keyed by seq.
///
/// The runtime owns the conversation; this mirror only holds what it last sent. Merging is
/// idempotent, so at least once delivery and resync overlap never duplicate a row. A seq
/// beyond the contiguous run marks a gap, and the client resyncs from contiguousHead.
/// Nothing here is persisted: a relaunch rebuilds it from the runtime.
public struct ConversationMirror: Equatable, Sendable {
    public private(set) var rows: [Int: Row] = [:]
    /// Highest seq n with every seq 0...n present; -1 when empty.
    public private(set) var contiguousHead = -1

    public init() {}

    public var ordered: [Row] { rows.keys.sorted().compactMap { rows[$0] } }
    public var headSeq: Int { rows.keys.max() ?? -1 }
    public var hasGap: Bool { headSeq > contiguousHead }

    /// Merge rows; return how many were new.
    @discardableResult
    public mutating func merge(_ incoming: [Row]) -> Int {
        var added = 0
        for row in incoming where rows[row.seq] == nil {
            rows[row.seq] = row
            added += 1
        }
        while rows[contiguousHead + 1] != nil {
            contiguousHead += 1
        }
        return added
    }

    public mutating func reset() {
        rows = [:]
        contiguousHead = -1
    }
}
