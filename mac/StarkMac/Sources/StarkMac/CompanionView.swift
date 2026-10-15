import StarkKit
import SwiftUI

/// Panel content: the chat card above Stark when open, Stark always at the bottom left so
/// the panel can grow upward without moving him.
struct CompanionView: View {
    @Bindable var model: StarkModel
    let controller: PanelController

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if model.chatOpen {
                ChatCard(model: model)
                    .frame(width: PanelController.cardSize.width,
                           height: PanelController.cardSize.height)
                    .transition(.opacity.combined(with: .move(edge: .bottom)))
            }
            body(for: model)
                .frame(width: model.avatarSize.width, height: model.avatarSize.height)
                .contentShape(Rectangle())
                .gesture(DragGesture(minimumDistance: 3, coordinateSpace: .global)
                    .onChanged { _ in controller.dragChanged() }
                    .onEnded { _ in controller.dragEnded() })
                .gesture(TapGesture(count: 2).onEnded { controller.openChat() }
                    .exclusively(before: TapGesture().onEnded { controller.toggleChat() }))
                .help("Click to talk to Stark, drag to move him, right click for options.")
        }
        .padding(PanelController.inset)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottomLeading)
    }

    @ViewBuilder
    private func body(for model: StarkModel) -> some View {
        if let stage = model.stage {
            SpriteAvatarView(stage: stage, size: model.avatarSize, ghost: model.avatar == .ghost,
                             clickThrough: model.clickThrough)
        } else {
            AvatarView(state: model.avatar, clickThrough: model.clickThrough)
        }
    }
}

struct ChatCard: View {
    @Bindable var model: StarkModel
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            StatusView(state: model.state)
            Divider()
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 6) {
                        ForEach(model.visibleRows) { row in
                            Bubble(row: row).id(row.seq)
                        }
                        ForEach(model.attention) { m in
                            PendingBubble(message: m)
                        }
                    }
                    .padding(.vertical, 2)
                }
                .onChange(of: model.state.rows.last?.seq) { _, seq in
                    if let seq { withAnimation { proxy.scrollTo(seq, anchor: .bottom) } }
                }
                .onAppear {
                    if let seq = model.visibleRows.last?.seq { proxy.scrollTo(seq, anchor: .bottom) }
                }
            }
            if !model.state.isActiveSurface && model.state.connection.isLive {
                Label("Stark is on \(model.state.presence?.surface ?? "another surface"). "
                      + "This panel is read only.", systemImage: "moon.zzz")
                    .font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                TextField("Message Stark", text: $model.draft)
                    .textFieldStyle(.roundedBorder)
                    .focused($focused)
                    .onSubmit { model.send() }
                    .disabled(!model.canSend)
                Button("Send") { model.send() }
                    .keyboardShortcut(.return, modifiers: [])
                    .disabled(!model.canSend || model.draft.isEmpty)
            }
        }
        .padding(12)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(.white.opacity(0.15)))
        .onChange(of: focused) { _, f in model.inputFocused = f }
        .onAppear { focused = true }
    }
}

struct StatusView: View {
    let state: ClientState

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Circle().fill(dot).frame(width: 8, height: 8)
                Text("Stark").font(.headline)
                if let uuid = state.identity?.uuid {
                    Text(String(uuid.prefix(8))).font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                        .help(uuid)
                }
                Spacer()
                Text(connectionText).font(.caption).foregroundStyle(.secondary)
            }
            if let p = state.presence {
                Text("presence \(p.surface) · generation \(p.generation)"
                     + (state.handoff.map { " · jack \($0.state) to \($0.to)" } ?? ""))
                    .font(.caption.monospaced()).foregroundStyle(.secondary)
            }
            if !state.responsibilities.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 4) {
                        ForEach(state.responsibilities) { r in
                            Text("\(r.id) \(r.state)")
                                .font(.caption2.monospaced())
                                .padding(.horizontal, 6).padding(.vertical, 2)
                                .background(chip(r.state).opacity(0.25), in: Capsule())
                        }
                    }
                }
            }
        }
    }

    private var dot: Color {
        switch state.connection {
        case .live: state.isActiveSurface ? .green : .gray
        case .disconnected: .red
        default: .orange
        }
    }

    private var connectionText: String {
        switch state.connection {
        case .live: state.working ? "thinking" : "live"
        case .disconnected(let why): why ?? "offline"
        case .connecting: "connecting"
        case .authenticating: "authenticating"
        case .syncing: "syncing"
        }
    }

    private func chip(_ s: String) -> Color {
        switch s {
        case "active", "pending": .blue
        case "blocked": .red
        default: .gray
        }
    }
}

struct Bubble: View {
    let row: Row

    var body: some View {
        let mine = row.role == "user"
        HStack {
            if mine { Spacer(minLength: 30) }
            Text(row.content)
                .textSelection(.enabled)
                .padding(.horizontal, 10).padding(.vertical, 6)
                .background(mine ? Color.accentColor.opacity(0.85) : Color.secondary.opacity(0.18),
                            in: RoundedRectangle(cornerRadius: 12))
                .foregroundStyle(mine ? .white : .primary)
            if !mine { Spacer(minLength: 30) }
        }
    }
}

struct PendingBubble: View {
    let message: OutgoingMessage

    var body: some View {
        HStack {
            Spacer(minLength: 30)
            VStack(alignment: .trailing, spacing: 2) {
                Text(message.text)
                    .padding(.horizontal, 10).padding(.vertical, 6)
                    .background(Color.accentColor.opacity(0.35), in: RoundedRectangle(cornerRadius: 12))
                Text(caption).font(.caption2).foregroundStyle(.secondary)
            }
        }
    }

    private var caption: String {
        switch message.status {
        case .pending: "sending"
        case .rejected(let code): code == "stale_presence"
            ? "not sent: this panel no longer holds presence" : "not sent: \(code)"
        case .sent: ""
        }
    }
}
