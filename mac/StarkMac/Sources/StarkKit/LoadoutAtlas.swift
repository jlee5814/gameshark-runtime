import CoreGraphics
import Foundation
import ImageIO

/// Crops one CGImage per manifest frame from an atlas file. Both loadouts share the
/// manifest's layout, so frame N of the gym atlas is frame N of the standard one with
/// the cap and earbuds baked in, and animation timing never depends on the loadout.
public enum AtlasCrop {
    /// nil when the file is missing, unreadable, or not the manifest's atlas size.
    public static func frames(at url: URL, manifest m: SpriteManifest) -> [CGImage]? {
        guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
              let atlas = CGImageSourceCreateImageAtIndex(src, 0, nil),
              atlas.width == m.atlas.width, atlas.height == m.atlas.height
        else { return nil }
        var out: [CGImage] = []
        out.reserveCapacity(m.frames.count)
        for f in m.frames {
            let r = CGRect(x: f.atlas[0], y: f.atlas[1], width: f.atlas[2], height: f.atlas[3])
            guard let img = atlas.cropping(to: r) else { return nil }
            out.append(img)
        }
        return out
    }
}

/// The frames a stage draws, per loadout. A missing gym atlas draws the standard sprite.
public struct LoadoutFrames {
    public let standard: [CGImage]
    public let gym: [CGImage]?

    public init(standard: [CGImage], gym: [CGImage]?) {
        self.standard = standard
        self.gym = gym?.count == standard.count ? gym : nil
    }

    public func frames(for loadout: Loadout) -> [CGImage] {
        switch loadout {
        case .standard: standard
        case .gym: gym ?? standard
        }
    }
}
