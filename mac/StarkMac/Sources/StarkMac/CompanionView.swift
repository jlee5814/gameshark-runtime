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
                    .exclusively(before: TapGesture().onEnded {
                        if !model.avatarTapped() { controller.toggleChat() }
                    }))
                .accessibilityAction(named: "Stop Speaking") { model.voice.stop() }
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
            HStack(alignment: .top) {
                StatusView(state: model.state, badge: model.modelBadge)
                VoiceControls(voice: model.voice)
                if model.state.memoryAvailable {
                    Button { model.showMemories.toggle() } label: {
                        Image(systemName: "brain")
                    }
                    .buttonStyle(.borderless)
                    .help("Saved memories (\(model.state.memories.count))")
                }
            }
            Divider()
            if model.showMemories {
                MemoryPanel(model: model)
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 6) {
                            ForEach(model.visibleRows) { row in
                                Bubble(row: row, voice: model.voice).id(row.seq)
                            }
                            ForEach(model.attention) { m in
                                PendingBubble(message: m)
                            }
                            if let line = model.replyLine {
                                ReplyLine(text: line, failed: model.state.reply?.failed == true,
                                          canRetry: model.canRetryReply) { model.retryReply() }
                                    .id("reply")
                            }
                        }
                        .padding(.vertical, 2)
                    }
                    .onChange(of: model.state.rows.last?.seq) { _, seq in
                        if let seq { withAnimation { proxy.scrollTo(seq, anchor: .bottom) } }
                    }
                    // The reply line sits below the newest row and changes without a new row;
                    // keep it, and its Retry button, in view.
                    .onChange(of: model.replyLine) { _, line in
                        if line != nil { withAnimation { proxy.scrollTo("reply", anchor: .bottom) } }
                    }
                    .onAppear {
                        if let seq = model.visibleRows.last?.seq { proxy.scrollTo(seq, anchor: .bottom) }
                    }
                }
            }
            if !model.state.isActiveSurface && model.state.connection.isLive {
                HStack {
                    Label("Stark is on \(model.state.holderName ?? "another surface"). "
                          + "This panel is read only.", systemImage: "moon.zzz")
                        .font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    if model.canJackIn {
                        Button("Jack In Here") { model.jackIn() }
                            .controlSize(.small)
                    }
                }
            }
            if let offer = model.draftOffer {
                VStack(alignment: .leading, spacing: 6) {
                    Label("Stark brought unsent text", systemImage: "text.bubble")
                        .font(.caption).foregroundStyle(.secondary)
                    Text(offer.text).font(.callout).lineLimit(3)
                    HStack {
                        Button("Use It") { model.useCarriedDraft() }
                        Button("Keep Mine") { model.keepCurrentDraft() }
                    }
                    .controlSize(.small)
                }
                .padding(8)
                .background(.quaternary, in: RoundedRectangle(cornerRadius: 8))
            }
            VoiceDraftCard(listen: model.listen, canSend: model.canSend)
            HStack {
                PushToTalkButton(listen: model.listen)
                TextField("Message Stark, or /remember a fact", text: $model.draft)
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
        .onAppear {
            focused = true
            model.listen.refresh()
        }
        .onExitCommand { model.listen.cancel(.escape) }
    }
}

/// Hold to talk. The microphone opens on press and closes on release; nothing is sent.
struct PushToTalkButton: View {
    let listen: CaptureCoordinator
    /// True while the finger or pointer is down; resets on its own when the system cancels
    /// the gesture (a permission prompt, Control Center), which also releases.
    @GestureState private var pressing = false

    var body: some View {
        let on = listen.isListening
        Image(systemName: on ? "mic.fill" : "mic")
            .foregroundStyle(on ? Color.red : listen.canPress ? Color.primary : Color.secondary)
            .frame(width: 22, height: 22)
            .contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 0)
                .updating($pressing) { _, down, _ in down = true })
            .onChange(of: pressing) { _, down in
                if down { listen.press() } else { listen.release() }
            }
            .allowsHitTesting(listen.isPresent || on)
            .help(listen.availability.explanation ?? "Hold to talk to Stark")
            .accessibilityElement()
            .accessibilityLabel(on ? "Listening" : "Hold to talk")
            .accessibilityAddTraits(.isButton)
            .accessibilityAction(named: "Start Listening") { listen.press() }
            .accessibilityAction(named: "Stop Listening") { listen.release() }
    }
}

/// The voice draft: live while listening, editable in review, sent or inserted only on
/// an explicit choice. It never enters the composer's draft or travels with Stark.
struct VoiceDraftCard: View {
    let listen: CaptureCoordinator
    let canSend: Bool

    var body: some View {
        let text = Binding(get: { listen.draft }, set: { listen.edit($0) })
        Group {
            switch listen.panel {
            case .listening:
                VStack(alignment: .leading, spacing: 4) {
                    Label("Listening. Release to review, Escape to cancel.", systemImage: "waveform")
                        .font(.caption).foregroundStyle(.secondary)
                    Text(listen.draft.isEmpty ? "Speak now" : listen.draft)
                        .font(.callout)
                        .foregroundStyle(listen.draft.isEmpty ? .secondary : .primary)
                }
                .card()
            case .reviewing:
                VStack(alignment: .leading, spacing: 6) {
                    Label("Voice draft: edit, then send or insert", systemImage: "mic")
                        .font(.caption).foregroundStyle(.secondary)
                    TextField("Voice draft", text: text, axis: .vertical)
                        .textFieldStyle(.roundedBorder)
                    HStack {
                        Button("Send") { listen.send() }
                            .disabled(!canSend || listen.draft.isEmpty)
                        Button("Insert into Composer") { listen.insertIntoComposer() }
                            .disabled(listen.draft.isEmpty)
                        Button("Discard") { listen.cancel(.user) }
                    }
                    .controlSize(.small)
                }
                .card()
            case .requesting:
                Label("Allow microphone and speech recognition, then hold the microphone again.",
                      systemImage: "lock.shield")
                    .font(.caption).foregroundStyle(.secondary)
            case .note(let note):
                Label(note, systemImage: "mic.slash")
                    .font(.caption).foregroundStyle(.secondary)
            case .none:
                EmptyView()
            }
        }
    }
}

private extension View {
    func card() -> some View {
        frame(maxWidth: .infinity, alignment: .leading)
            .padding(8)
            .background(.quaternary, in: RoundedRectangle(cornerRadius: 8))
    }
}

struct StatusView: View {
    let state: ClientState
    var badge: String?

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
                if let badge {
                    Text(badge).font(.caption2.weight(.semibold))
                        .padding(.horizontal, 6).padding(.vertical, 1)
                        .background(.orange.opacity(0.3), in: Capsule())
                        .help("Replies come from the demo model, not a real one")
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
        case .live: state.isWorking ? "working" : state.isThinking ? "thinking" : "live"
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
    let voice: SpeechCoordinator

    var body: some View {
        let mine = row.role == "user"
        let saying = voice.speakingSeq == row.seq
        HStack(alignment: .bottom, spacing: 4) {
            if mine { Spacer(minLength: 30) }
            Text(row.content)
                .textSelection(.enabled)
                .padding(.horizontal, 10).padding(.vertical, 6)
                .background(mine ? Color.accentColor.opacity(0.85) : Color.secondary.opacity(0.18),
                            in: RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12)
                    .stroke(Color.accentColor, lineWidth: saying ? 1.5 : 0))
                .foregroundStyle(mine ? .white : .primary)
            if !mine && voice.canReplay {
                Button {
                    if saying { voice.stop() } else { voice.replay(seq: row.seq) }
                } label: {
                    Image(systemName: saying ? "stop.circle" : "play.circle")
                }
                .buttonStyle(.borderless)
                .foregroundStyle(.secondary)
                .help(saying ? "Stop speaking" : "Read this reply aloud")
                .accessibilityLabel(saying ? "Stop speaking" : "Replay this reply aloud")
            }
            if !mine { Spacer(minLength: 30) }
        }
    }
}

/// Mute and Stop for Stark's voice on this Mac.
struct VoiceControls: View {
    let voice: SpeechCoordinator

    var body: some View {
        HStack(spacing: 6) {
            if voice.speakingSeq != nil {
                Button { voice.stop() } label: { Image(systemName: "stop.fill") }
                    .buttonStyle(.borderless)
                    .help("Stop speaking")
                    .accessibilityLabel("Stop speaking")
            }
            Button { voice.toggleMute() } label: {
                Image(systemName: voice.settings.muted ? "speaker.slash" : "speaker.wave.2")
            }
            .buttonStyle(.borderless)
            .help(voice.settings.muted ? "Unmute Stark's voice" : "Mute Stark's voice")
            .accessibilityLabel(voice.settings.muted ? "Unmute Stark's voice" : "Mute Stark's voice")
        }
    }
}

/// The reply's state under the newest message: never text in Stark's voice.
struct ReplyLine: View {
    let text: String
    let failed: Bool
    let canRetry: Bool
    let retry: () -> Void

    var body: some View {
        HStack(spacing: 6) {
            if failed {
                Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
            } else {
                ProgressView().controlSize(.mini)
            }
            Text(text).font(.caption).foregroundStyle(.secondary)
            if failed && canRetry {
                Button("Retry", action: retry).controlSize(.small)
            }
            Spacer(minLength: 0)
        }
        .padding(.leading, 4)
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
