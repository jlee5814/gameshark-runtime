import StarkKit
import SwiftUI

struct RootView: View {
    @Bindable var model: PocketModel

    var body: some View {
        if model.isPaired {
            MainView(model: model)
        } else {
            PairView(model: model)
        }
    }
}

// MARK: pairing

struct PairView: View {
    @Bindable var model: PocketModel
    @State private var link = ""

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text("On your Mac run `stark.py pair`, then scan its QR code with the "
                         + "Camera, or paste the link here.")
                        .font(.callout)
                }
                Section("Pairing link") {
                    TextField("starkpocket://pair?…", text: $link, axis: .vertical)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .accessibilityIdentifier("pairLinkField")
                    Button("Paste") { link = UIPasteboard.general.string ?? link }
                }
                Section("This iPhone's name on the Mac") {
                    TextField("Name", text: $model.deviceName)
                        .accessibilityIdentifier("deviceNameField")
                }
                Section {
                    Button {
                        Task { await model.pair(link) }
                    } label: {
                        if model.pairing { ProgressView() } else { Text("Pair with Stark") }
                    }
                    .disabled(model.pairing || link.isEmpty)
                    .accessibilityIdentifier("pairButton")
                    if let e = model.pairingError {
                        Text(e).foregroundStyle(.red).font(.callout)
                            .accessibilityIdentifier("pairingError")
                    }
                }
            }
            .navigationTitle("Pair Stark Pocket")
        }
    }
}

// MARK: main

struct MainView: View {
    @Bindable var model: PocketModel
    @FocusState private var focused: Bool
    @State private var settings = false

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                HeaderView(model: model)
                Divider()
                ConversationList(model: model)
                Composer(model: model, focused: $focused)
            }
            .navigationTitle("Stark")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                VoiceButtons(voice: model.voice)
                if model.memoryAvailable {
                    Button { model.showMemories = true } label: { Image(systemName: "brain") }
                        .accessibilityLabel("Saved memories")
                        .accessibilityIdentifier("memoriesButton")
                }
                Button { settings = true } label: { Image(systemName: "gearshape") }
                    .accessibilityIdentifier("settingsButton")
            }
            .sheet(isPresented: $settings) { SettingsView(model: model) }
            .sheet(isPresented: $model.showMemories) { MemoriesView(model: model) }
            // A sheet covers the composer and its Stop Listening action: close the mic first.
            .onChange(of: settings || model.showMemories) { _, covered in
                if covered { model.listen.stopListening(.covered) }
            }
            .onChange(of: focused) { _, f in model.inputFocused = f }
        }
    }
}

struct HeaderView: View {
    let model: PocketModel

    var body: some View {
        let summary = model.summary
        let active = model.state?.isActiveSurface ?? false
        HStack(alignment: .bottom, spacing: 16) {
            AvatarView(stage: model.stage, ghost: !active)
                .frame(width: 98, height: 119)
                .contentShape(Rectangle())
                .onTapGesture { model.voice.stop() }    // tap Stark to silence him
                .accessibilityAction(named: "Stop Speaking") { model.voice.stop() }
            VStack(alignment: .leading, spacing: 8) {
                Label(summary?.label ?? "Connecting", systemImage: active ? "bolt.fill" : "moon.zzz")
                    .font(.headline)
                    .accessibilityElement(children: .combine)
                    .accessibilityIdentifier("presenceLabel")
                HStack(spacing: 6) {
                    ConnectionChip(state: model.state)
                    if let badge = model.modelBadge {
                        Text(badge).font(.caption2.weight(.semibold))
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .background(.orange.opacity(0.3), in: Capsule())
                            .accessibilityIdentifier("modelBadge")
                    }
                }
                switch summary?.control ?? .none {
                case .jackIn:
                    Button("Jack In") { model.jack() }
                        .buttonStyle(.borderedProminent)
                        .accessibilityIdentifier("jackButton")
                case .jackOut:
                    Button("Jack Out") { model.jack() }
                        .buttonStyle(.bordered)
                        .accessibilityIdentifier("jackButton")
                case .none:
                    Button("Jack In") {}
                        .buttonStyle(.bordered)
                        .disabled(true)
                        .accessibilityIdentifier("jackButton")
                }
            }
            Spacer()
        }
        .padding()
    }
}

struct ConnectionChip: View {
    let state: ClientState?

    var body: some View {
        let (text, color): (String, Color) = switch state?.connection {
        case .live: (state?.isActiveSurface == true ? "live" : "read only", state?.isActiveSurface == true ? .green : .gray)
        case .disconnected(let why): (why ?? "offline", .red)
        default: ("connecting", .orange)
        }
        Text(text)
            .font(.caption.weight(.medium))
            .padding(.horizontal, 8).padding(.vertical, 3)
            .background(color.opacity(0.2), in: Capsule())
            .accessibilityIdentifier("connectionLabel")
    }
}

struct AvatarView: View {
    let stage: PocketStage?
    var ghost: Bool

    var body: some View {
        Group {
            if let stage {
                Image(decorative: stage.image, scale: 1)
                    .interpolation(.none)
                    .resizable()
                    .aspectRatio(contentMode: .fit)
                    .accessibilityLabel(stage.showsGym ? "Stark, gym"
                                                       : "Stark, \(stage.animation.rawValue)")
            } else {
                Image(systemName: "person.crop.circle").resizable().aspectRatio(contentMode: .fit)
            }
        }
        .saturation(ghost ? 0 : 1)
        .opacity(ghost ? 0.45 : 1)
        .accessibilityIdentifier("avatar")
    }
}

struct ConversationList: View {
    let model: PocketModel

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(model.visibleRows) { r in
                        Bubble(row: r, voice: model.voice).id(r.seq)
                    }
                    ForEach(model.pendingOutbox) { m in
                        Text(m.status == .pending ? "sending: \(m.text)" : "not sent: \(m.text)")
                            .font(.caption).foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .trailing)
                    }
                    if let line = model.replyLine {
                        ReplyLine(model: model, text: line).id("reply")
                    }
                }
                .padding()
            }
            .onChange(of: model.visibleRows.last?.seq) { _, seq in
                if let seq { withAnimation { proxy.scrollTo(seq, anchor: .bottom) } }
                model.markViewed()
            }
            // The reply line sits below the newest row and changes without a new row; keep
            // it, and its Retry button, in view.
            .onChange(of: model.replyLine) { _, line in
                if line != nil { withAnimation { proxy.scrollTo("reply", anchor: .bottom) } }
            }
            .onAppear { model.markViewed() }
        }
    }
}

/// The reply's state under the newest message: never text in Stark's voice.
struct ReplyLine: View {
    let model: PocketModel
    let text: String

    var body: some View {
        let failed = model.state?.reply?.failed == true
        HStack(spacing: 8) {
            if failed {
                Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
            } else {
                ProgressView()
            }
            Text(text).font(.caption).foregroundStyle(.secondary)
                .accessibilityIdentifier("replyLine")
            Spacer(minLength: 0)
            if failed && model.canRetryReply {
                Button("Retry") { model.retryReply() }
                    .buttonStyle(.bordered).controlSize(.small)
                    .accessibilityIdentifier("retryButton")
            }
        }
    }
}

struct Bubble: View {
    let row: Row
    let voice: SpeechCoordinator

    var body: some View {
        let mine = row.role == "user"
        let saying = voice.speakingSeq == row.seq
        HStack(alignment: .bottom, spacing: 6) {
            Text(row.content)
                .padding(10)
                .background(mine ? Color.accentColor.opacity(0.85) : Color.secondary.opacity(0.18),
                            in: RoundedRectangle(cornerRadius: 14))
                .overlay(RoundedRectangle(cornerRadius: 14)
                    .stroke(Color.accentColor, lineWidth: saying ? 2 : 0))
                .foregroundStyle(mine ? .white : .primary)
                .accessibilityIdentifier("row-\(row.seq)")
                .contextMenu {
                    if !mine && voice.canReplay {
                        Button { voice.replay(seq: row.seq) } label: {
                            Label("Read Aloud", systemImage: "play.circle")
                        }
                    }
                }
            if !mine && voice.canReplay {
                Button {
                    if saying { voice.stop() } else { voice.replay(seq: row.seq) }
                } label: {
                    Image(systemName: saying ? "stop.circle" : "play.circle")
                        .imageScale(.large)
                }
                .buttonStyle(.borderless)
                .foregroundStyle(.secondary)
                .accessibilityLabel(saying ? "Stop speaking" : "Replay this reply aloud")
                .accessibilityIdentifier(saying ? "stopReplayButton-\(row.seq)"
                                                : "replayButton-\(row.seq)")
            }
        }
        .frame(maxWidth: .infinity, alignment: mine ? .trailing : .leading)
    }
}

/// Mute and Stop in the toolbar.
struct VoiceButtons: View {
    let voice: SpeechCoordinator

    var body: some View {
        if voice.speakingSeq != nil {
            Button { voice.stop() } label: { Image(systemName: "stop.fill") }
                .accessibilityLabel("Stop speaking")
                .accessibilityIdentifier("stopSpeechButton")
        }
        Button { voice.toggleMute() } label: {
            Image(systemName: voice.settings.muted ? "speaker.slash" : "speaker.wave.2")
        }
        .accessibilityLabel(voice.settings.muted ? "Unmute Stark's voice" : "Mute Stark's voice")
        .accessibilityIdentifier("muteButton")
    }
}

/// This iPhone's voice settings: never part of Stark's persona, never sent to the Mac.
struct VoiceSection: View {
    let voice: SpeechCoordinator
    @State private var options: [VoiceOption] = []
    @State private var fallback: String?

    var body: some View {
        Section {
            Toggle("Mute Stark", isOn: Binding(get: { voice.settings.muted },
                                               set: { m in voice.setMuted(m) }))
                .accessibilityIdentifier("muteToggle")
            Picker("Voice", selection: Binding(
                get: { voice.settings.voiceIdentifier ?? "" },
                set: { id in voice.update { $0.voiceIdentifier = id.isEmpty ? nil : id } })) {
                Text("Default" + (fallback.map { " (\($0))" } ?? "")).tag("")
                ForEach(options) { Text($0.label).tag($0.id) }
            }
            .accessibilityIdentifier("voicePicker")
            LabeledContent("Speed") {
                Slider(value: Binding(get: { voice.settings.rate },
                                      set: { r in voice.update { $0.rate = r } }),
                       in: VoiceSettings.rateRange)
                    .accessibilityLabel("Speaking speed")
            }
            LabeledContent("Pitch") {
                Slider(value: Binding(get: { voice.settings.pitch },
                                      set: { p in voice.update { $0.pitch = p } }),
                       in: VoiceSettings.pitchRange)
                    .accessibilityLabel("Voice pitch")
            }
        } header: {
            Text("Voice")
        } footer: {
            Text("Stark reads his new replies aloud only while he is on this iPhone. Better "
                 + "voices download in Settings, Accessibility, Spoken Content, Voices.")
        }
        .onAppear {
            options = VoiceCatalog.options()
            fallback = VoiceCatalog.defaultOption?.name
        }
    }
}

struct Composer: View {
    @Bindable var model: PocketModel
    var focused: FocusState<Bool>.Binding

    var body: some View {
        let canSend = model.summary?.canSend ?? false
        VStack(spacing: 0) {
            if let offer = model.draftOffer {
                VStack(alignment: .leading, spacing: 6) {
                    Label("Stark brought unsent text", systemImage: "text.bubble")
                        .font(.caption).foregroundStyle(.secondary)
                    Text(offer.text).font(.callout).lineLimit(3)
                        .accessibilityIdentifier("draftOfferText")
                    HStack {
                        Button("Use It") { model.useCarriedDraft() }
                            .accessibilityIdentifier("useDraftButton")
                        Button("Keep Mine") { model.keepCurrentDraft() }
                            .accessibilityIdentifier("keepDraftButton")
                    }
                    .buttonStyle(.bordered)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(10)
                .background(.quaternary, in: RoundedRectangle(cornerRadius: 10))
                .padding(.horizontal)
            }
            VoiceDraftCard(listen: model.listen, canSend: canSend)
            HStack {
                PushToTalkButton(listen: model.listen)
                TextField(canSend ? "Message Stark, or /remember a fact" : "Jack In to talk to Stark here",
                          text: $model.draft, axis: .vertical)
                    .lineLimit(1...5)
                    .fixedSize(horizontal: false, vertical: true)
                    .textFieldStyle(.roundedBorder)
                    .focused(focused)
                    .disabled(!canSend)
                    .accessibilityIdentifier("composer")
                Button("Send") { model.send() }
                    .disabled(!canSend || model.draft.isEmpty)
                    .accessibilityIdentifier("sendButton")
            }
            .padding()
        }
        .onAppear { model.listen.refresh() }
    }
}

/// Hold to talk. The microphone opens on press and closes on release; nothing is sent.
struct PushToTalkButton: View {
    let listen: CaptureCoordinator
    /// True while the finger or pointer is down; resets on its own when the system cancels
    /// the gesture (a permission prompt, Control Center), which also releases.
    @GestureState private var pressing = false

    var body: some View {
        let mic = listen.mic
        Group {
            if mic == .finishing {
                ProgressView()
            } else {
                Image(systemName: mic == .recording ? "mic.fill" : "mic")
                    .font(.title3)
                    .foregroundStyle(mic == .recording ? Color.red
                                     : mic == .ready ? Color.accentColor : Color.secondary)
            }
        }
            .frame(width: 36, height: 36)
            .contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 0)
                .updating($pressing) { _, down, _ in down = true })
            .onChange(of: pressing) { _, down in
                if down { listen.press() } else { listen.release() }
            }
            // A held press can always release; nothing lands while recognition finishes.
            .allowsHitTesting(mic == .recording || listen.isPresent && mic != .finishing)
            .accessibilityElement()
            .accessibilityLabel(mic == .recording ? "Listening"
                                : mic == .finishing ? "Finishing recognition" : "Hold to talk")
            .accessibilityAddTraits(.isButton)
            .accessibilityAction(named: "Start Listening") { listen.press() }
            .accessibilityAction(named: "Stop Listening") { listen.release() }
            .accessibilityIdentifier("pushToTalkButton")
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
                    Label("Listening. Release to review.", systemImage: "waveform")
                        .font(.caption).foregroundStyle(.secondary)
                    Text(listen.draft.isEmpty ? "Speak now" : listen.draft)
                        .foregroundStyle(listen.draft.isEmpty ? .secondary : .primary)
                        .accessibilityIdentifier("voiceDraftLive")
                }
                .voiceCard()
            case .finishing:
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.mini)
                        Text("Microphone off. Finishing recognition…")
                    }
                    .font(.caption).foregroundStyle(.secondary)
                    .accessibilityIdentifier("voiceFinishing")
                    if !listen.draft.isEmpty {
                        Text(listen.draft).accessibilityIdentifier("voiceDraftLive")
                    }
                }
                .voiceCard()
            case .reviewing:
                VStack(alignment: .leading, spacing: 8) {
                    Label("Voice draft: edit, then send or insert", systemImage: "mic")
                        .font(.caption).foregroundStyle(.secondary)
                    // A vertical field yields height before anything else in the stack, so
                    // the keyboard squeezed it to nothing; cap it and keep it at that size.
                    TextField("Voice draft", text: text, axis: .vertical)
                        .lineLimit(1...5)
                        .fixedSize(horizontal: false, vertical: true)
                        .textFieldStyle(.roundedBorder)
                        .accessibilityIdentifier("voiceDraftText")
                    HStack {
                        Button("Send") { listen.send() }
                            .disabled(!canSend || listen.draft.isEmpty)
                            .accessibilityIdentifier("voiceSendButton")
                        Button("Insert into Composer") { listen.insertIntoComposer() }
                            .disabled(listen.draft.isEmpty)
                            .accessibilityIdentifier("voiceInsertButton")
                        Spacer()
                        Button("Discard", role: .destructive) { listen.cancel(.user) }
                            .accessibilityIdentifier("voiceDiscardButton")
                    }
                    .buttonStyle(.bordered)
                }
                .voiceCard()
            case .requesting:
                Label("Allow microphone and speech recognition, then hold the microphone again.",
                      systemImage: "lock.shield")
                    .font(.caption).foregroundStyle(.secondary)
                    .padding(.horizontal)
            case .note(let note):
                Label(note, systemImage: "mic.slash")
                    .font(.caption).foregroundStyle(.secondary)
                    .padding(.horizontal)
                    .accessibilityIdentifier("voiceNotice")
            case .none:
                EmptyView()
            }
        }
    }
}

private extension View {
    func voiceCard() -> some View {
        frame(maxWidth: .infinity, alignment: .leading)
            .padding(10)
            .background(.quaternary, in: RoundedRectangle(cornerRadius: 10))
            .padding(.horizontal)
    }
}

/// Saved memories: the runtime's list, an Add field, swipe to forget, tap to correct.
/// Every change is a typed command to the Mac; the phone keeps no copy of its own.
struct MemoriesView: View {
    @Bindable var model: PocketModel
    @Environment(\.dismiss) private var dismiss
    @State private var adding = ""
    @State private var editing: MemoryItem?
    @State private var editText = ""

    var body: some View {
        NavigationStack {
            List {
                if !model.memoryInject {
                    Text("Memory is off on the Mac: saved facts do not reach the model.")
                        .font(.callout).foregroundStyle(.orange)
                }
                Section {
                    if model.memories.isEmpty && model.memoryOps.isEmpty {
                        Text("Nothing saved yet.").foregroundStyle(.secondary)
                    }
                    ForEach(model.memories) { m in
                        Button {
                            guard model.canWriteMemory else { return }
                            editText = m.content
                            editing = m
                        } label: {
                            Text(m.content).foregroundStyle(.primary)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .accessibilityIdentifier("memory-\(m.content)")
                        .swipeActions {
                            Button("Forget", role: .destructive) { model.forgetMemory(m.id) }
                                .disabled(!model.canForgetMemory)
                                .accessibilityIdentifier("forgetButton")
                        }
                    }
                    ForEach(model.memoryOps) { op in
                        if let text = op.text {
                            HStack {
                                Text(text).foregroundStyle(.secondary)
                                Spacer()
                                if op.status == .pending {
                                    ProgressView()
                                } else {
                                    Button("Dismiss") { model.dismissMemoryOp(op.id) }
                                }
                            }
                        }
                    }
                } footer: {
                    Text("Forgetting removes a fact from saved memory. Earlier chat that "
                         + "mentions it stays in the conversation.")
                }
                if let notice = model.memoryNotice {
                    Text(notice).font(.callout)
                        .accessibilityIdentifier("memoryNotice")
                }
                Section(model.canWriteMemory ? "Add" : "Jack In to add or correct") {
                    TextField("A fact for Stark to keep", text: $adding, axis: .vertical)
                        .disabled(!model.canWriteMemory)
                        .accessibilityIdentifier("memoryField")
                    Button("Save") {
                        model.addMemory(adding)
                        adding = ""
                    }
                    .disabled(!model.canWriteMemory || adding.isEmpty)
                    .accessibilityIdentifier("saveMemoryButton")
                }
            }
            .navigationTitle("Memories")
            .toolbar { Button("Done") { dismiss() } }
            .sheet(item: $editing) { m in
                NavigationStack {
                    Form {
                        TextField("Memory", text: $editText, axis: .vertical)
                            .accessibilityIdentifier("editMemoryField")
                    }
                    .navigationTitle("Correct Memory")
                    .toolbar {
                        ToolbarItem(placement: .cancellationAction) {
                            Button("Cancel") { editing = nil }
                        }
                        ToolbarItem(placement: .confirmationAction) {
                            Button("Save") {
                                model.correctMemory(m, text: editText)
                                editing = nil
                            }
                            .accessibilityIdentifier("saveCorrectionButton")
                        }
                    }
                }
            }
        }
    }
}

struct SettingsView: View {
    let model: PocketModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                if let s = model.state {
                    Section("Stark") {
                        LabeledContent("Identity", value: String(s.identity?.uuid.prefix(8) ?? "…"))
                        LabeledContent("This surface", value: s.surface)
                        LabeledContent("Presence", value: s.presence.map { "\($0.surface) gen \($0.generation)" } ?? "…")
                    }
                }
                VoiceSection(voice: model.voice)
                Section("Diagnostics") {
                    LabeledContent("Build", value: NetLog.build)
                    LabeledContent("Network", value: model.networkLabel)
                    ForEach(model.routeSummary, id: \.self) { Text($0).font(.caption.monospaced()) }
                }
                Section {
                    Button(model.unpairing ? "Sending Stark home…" : "Unpair This iPhone",
                           role: .destructive) {
                        Task { if await model.unpair() { dismiss() } }
                    }
                    .disabled(model.unpairing)
                    .accessibilityIdentifier("unpairButton")
                    if let why = model.unpairError {
                        Text(why)
                            .foregroundStyle(.red)
                            .accessibilityIdentifier("unpairError")
                        Button("Forget Anyway", role: .destructive) {
                            model.forgetAnyway()
                            dismiss()
                        }
                    }
                } footer: {
                    Text("If Stark is here, he goes home to the Mac first; the pairing and "
                         + "device key are forgotten only once he arrives. To cut the phone "
                         + "off from the Mac too, run stark.py devices revoke on the Mac.")
                }
            }
            .navigationTitle("Settings")
            .toolbar { Button("Done") { dismiss() } }
        }
    }
}
