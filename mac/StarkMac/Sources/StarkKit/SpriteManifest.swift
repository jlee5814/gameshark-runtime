import Foundation

/// The atlas description written by mac/tools/extract_sprites.py. Every frame is a full
/// canvas tile with Stark's feet on the shared anchor, so drawing tile after tile never
/// shifts him.
public struct SpriteManifest: Decodable, Sendable {
    public struct Size: Decodable, Sendable { public var width: Int; public var height: Int }
    public struct Point: Decodable, Sendable { public var x: Int; public var y: Int }

    public struct Atlas: Decodable, Sendable {
        public var file: String
        public var width: Int
        public var height: Int
    }

    public struct Frame: Decodable, Sendable {
        public var id: String
        /// x, y, width, height of this frame's tile in the atlas.
        public var atlas: [Int]
    }

    public struct Clip: Decodable, Sendable {
        public var frames: [String]
        public var fps: Double
        public var loop: Bool
        /// Where a looping clip restarts, so an intro plays once (notify hops, then waves).
        public var loopFrom: Int?
        public var fallback: String?
    }

    public var version: Int
    public var atlas: Atlas
    public var canvas: Size
    public var anchor: Point
    public var frames: [Frame]
    public var animations: [String: Clip]

    public enum Invalid: Error, Equatable {
        case frameOutsideAtlas(String)
        case unknownFrame(String, String)
        case emptyClip(String)
        case badTiming(String)
        case missingAnimation(String)
    }

    public static func load(from url: URL) throws -> SpriteManifest {
        let m = try Wire.decode(SpriteManifest.self, from: Data(contentsOf: url))
        try m.validate()
        return m
    }

    public func validate() throws {
        for f in frames {
            guard f.atlas.count == 4, f.atlas[0] >= 0, f.atlas[1] >= 0,
                  f.atlas[0] + f.atlas[2] <= atlas.width, f.atlas[1] + f.atlas[3] <= atlas.height,
                  f.atlas[2] == canvas.width, f.atlas[3] == canvas.height
            else { throw Invalid.frameOutsideAtlas(f.id) }
        }
        let ids = Set(frames.map(\.id))
        for (name, clip) in animations {
            guard !clip.frames.isEmpty else { throw Invalid.emptyClip(name) }
            guard clip.fps > 0, (clip.loopFrom ?? 0) < clip.frames.count else {
                throw Invalid.badTiming(name)
            }
            if let bad = clip.frames.first(where: { !ids.contains($0) }) {
                throw Invalid.unknownFrame(name, bad)
            }
        }
        for a in StarkAnimation.allCases where animations[a.rawValue] == nil {
            throw Invalid.missingAnimation(a.rawValue)
        }
    }

    /// Index of each frame id in `frames`, for clips.
    public var frameIndex: [String: Int] {
        Dictionary(uniqueKeysWithValues: frames.enumerated().map { ($1.id, $0) })
    }
}
