import StarkKit
import SwiftUI

/// Stark's body: the current atlas frame drawn with nearest neighbor sampling at an
/// integer number of device pixels per sheet pixel, so edges stay hard at 1x, 2x, and 3x.
/// Every frame shares one canvas and foot anchor, so swapping frames never shifts him.
struct SpriteAvatarView: View {
    let stage: SpriteStage
    let size: CGSize
    var ghost = false
    var clickThrough = false

    var body: some View {
        Image(decorative: stage.image, scale: 1)
            .interpolation(.none)
            .antialiased(false)
            .resizable()
            .frame(width: size.width, height: size.height)
            .saturation(ghost ? 0 : 1)
            .opacity(ghost ? 0.45 : 1)
            .overlay(alignment: .bottom) {
                if clickThrough {
                    Capsule().strokeBorder(.white.opacity(0.6), style: StrokeStyle(lineWidth: 1.5, dash: [4, 4]))
                        .frame(width: size.width * 0.6, height: 10)
                }
            }
            .accessibilityElement()
            .accessibilityLabel("Stark, \(stage.animation.rawValue)")
    }
}
