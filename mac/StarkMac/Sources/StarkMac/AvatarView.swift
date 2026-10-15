import StarkKit
import SwiftUI

/// Stark's original avatar: a rounded core with a dark visor and two eye lights, drawn in
/// vector each frame. Idle breathes and blinks, listening widens the eyes and sends pulse
/// rings, working orbits three sparks, notification bounces a badge, ghost fades to a gray
/// outline, and disconnected goes still with closed eyes.
struct AvatarView: View {
    let state: AvatarState
    var clickThrough = false

    var body: some View {
        TimelineView(.animation(minimumInterval: 1 / 60, paused: state == .disconnected)) { tl in
            Canvas { ctx, size in
                draw(&ctx, size: size, t: tl.date.timeIntervalSinceReferenceDate)
            }
        }
        .saturation(state == .ghost || state == .disconnected ? 0 : 1)
        .opacity(state == .ghost ? 0.45 : 1)
        .accessibilityElement()
        .accessibilityLabel("Stark, \(state.rawValue)")
    }

    private var palette: (top: Color, bottom: Color, eye: Color) {
        switch state {
        case .working: (Color(red: 0.36, green: 0.42, blue: 0.95), Color(red: 0.20, green: 0.16, blue: 0.55), .white)
        case .notification: (Color(red: 0.98, green: 0.62, blue: 0.30), Color(red: 0.62, green: 0.25, blue: 0.40), .white)
        case .listening: (Color(red: 0.26, green: 0.82, blue: 0.74), Color(red: 0.10, green: 0.40, blue: 0.55), .white)
        default: (Color(red: 0.30, green: 0.72, blue: 0.86), Color(red: 0.16, green: 0.30, blue: 0.62), Color(red: 0.70, green: 0.98, blue: 1.0))
        }
    }

    private func draw(_ ctx: inout GraphicsContext, size: CGSize, t: Double) {
        let side = min(size.width, size.height)
        let center = CGPoint(x: size.width / 2, y: size.height / 2)
        let breathe = state == .disconnected ? 1 : 1 + 0.025 * sin(t * 1.6)
        let bounce = state == .notification ? -abs(sin(t * 4)) * side * 0.04 : 0
        let core = side * 0.62 * breathe
        let rect = CGRect(x: center.x - core / 2, y: center.y - core / 2 + bounce,
                          width: core, height: core)
        let colors = palette

        if state == .listening {
            for i in 0..<2 {
                let phase = (t * 0.9 + Double(i) * 0.5).truncatingRemainder(dividingBy: 1)
                let r = core * (0.55 + 0.35 * phase)
                let ring = Path(ellipseIn: CGRect(x: center.x - r, y: center.y - r + bounce,
                                                  width: 2 * r, height: 2 * r))
                ctx.stroke(ring, with: .color(colors.top.opacity(0.6 * (1 - phase))), lineWidth: 2)
            }
        }

        // Soft shadow, then the core.
        var shadow = ctx
        shadow.addFilter(.shadow(color: .black.opacity(0.35), radius: side * 0.05, y: side * 0.03))
        let body = Path(roundedRect: rect, cornerRadius: core * 0.32)
        shadow.fill(body, with: .linearGradient(
            Gradient(colors: [colors.top, colors.bottom]),
            startPoint: CGPoint(x: rect.midX, y: rect.minY),
            endPoint: CGPoint(x: rect.midX, y: rect.maxY)))
        ctx.stroke(body, with: .color(.white.opacity(0.25)), lineWidth: 1)

        // Antenna nub.
        let nub = CGRect(x: rect.midX - core * 0.05, y: rect.minY - core * 0.12,
                         width: core * 0.1, height: core * 0.1)
        ctx.fill(Path(ellipseIn: nub), with: .color(colors.top))

        // Visor.
        let visor = CGRect(x: rect.minX + core * 0.14, y: rect.minY + core * 0.30,
                           width: core * 0.72, height: core * 0.34)
        ctx.fill(Path(roundedRect: visor, cornerRadius: visor.height / 2),
                 with: .color(Color(red: 0.05, green: 0.07, blue: 0.14).opacity(0.92)))

        if state == .working {
            let sweep = (t * 1.2).truncatingRemainder(dividingBy: 1)
            let x = visor.minX + visor.width * sweep
            ctx.fill(Path(CGRect(x: x - core * 0.04, y: visor.minY, width: core * 0.08,
                                 height: visor.height)),
                     with: .color(colors.eye.opacity(0.18)))
        }

        // Eyes: blink every few seconds; closed lines when disconnected.
        let blinkPhase = t.truncatingRemainder(dividingBy: 4.2)
        let open: Double = switch state {
        case .disconnected: 0.08
        case .listening: 1.25
        default: blinkPhase < 0.12 ? 0.1 : 1
        }
        let eyeW = core * 0.12
        let eyeH = core * 0.14 * open
        for dx in [-0.17, 0.17] {
            let c = CGPoint(x: visor.midX + core * dx, y: visor.midY)
            let eye = CGRect(x: c.x - eyeW / 2, y: c.y - eyeH / 2, width: eyeW, height: max(eyeH, 1.5))
            var glow = ctx
            glow.addFilter(.blur(radius: 2))
            glow.fill(Path(ellipseIn: eye.insetBy(dx: -2, dy: -2)),
                      with: .color(colors.eye.opacity(0.5)))
            ctx.fill(Path(ellipseIn: eye), with: .color(colors.eye))
        }

        // Mouth line: a small smile when idle or notifying.
        if state == .idle || state == .notification {
            var mouth = Path()
            mouth.move(to: CGPoint(x: rect.midX - core * 0.1, y: rect.minY + core * 0.76))
            mouth.addQuadCurve(to: CGPoint(x: rect.midX + core * 0.1, y: rect.minY + core * 0.76),
                               control: CGPoint(x: rect.midX, y: rect.minY + core * 0.83))
            ctx.stroke(mouth, with: .color(.white.opacity(0.7)), lineWidth: 2)
        }

        if state == .working {
            for i in 0..<3 {
                let a = t * 2.4 + Double(i) * 2 * .pi / 3
                let r = core * 0.68
                let p = CGPoint(x: center.x + r * cos(a), y: center.y + r * sin(a) * 0.55)
                ctx.fill(Path(ellipseIn: CGRect(x: p.x - 4, y: p.y - 4, width: 8, height: 8)),
                         with: .color(colors.top))
            }
        }

        if state == .notification {
            let b = CGRect(x: rect.maxX - core * 0.16, y: rect.minY - core * 0.06,
                           width: core * 0.24, height: core * 0.24)
            ctx.fill(Path(ellipseIn: b), with: .color(Color(red: 1, green: 0.3, blue: 0.3)))
            ctx.draw(Text("!").font(.system(size: core * 0.16, weight: .heavy)).foregroundColor(.white),
                     at: CGPoint(x: b.midX, y: b.midY))
        }

        if clickThrough {
            let ring = Path(roundedRect: rect.insetBy(dx: -6, dy: -6), cornerRadius: core * 0.36)
            ctx.stroke(ring, with: .color(.white.opacity(0.5)),
                       style: StrokeStyle(lineWidth: 1.5, dash: [4, 4]))
        }
    }
}
