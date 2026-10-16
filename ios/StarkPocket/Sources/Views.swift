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
                Button { settings = true } label: { Image(systemName: "gearshape") }
                    .accessibilityIdentifier("settingsButton")
            }
            .sheet(isPresented: $settings) { SettingsView(model: model) }
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
            VStack(alignment: .leading, spacing: 8) {
                Label(summary?.label ?? "Connecting", systemImage: active ? "bolt.fill" : "moon.zzz")
                    .font(.headline)
                    .accessibilityElement(children: .combine)
                    .accessibilityIdentifier("presenceLabel")
                ConnectionChip(state: model.state)
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
                    .accessibilityLabel("Stark, \(stage.animation.rawValue)")
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
                        Bubble(row: r).id(r.seq)
                    }
                    ForEach(model.pendingOutbox) { m in
                        Text(m.status == .pending ? "sending: \(m.text)" : "not sent: \(m.text)")
                            .font(.caption).foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .trailing)
                    }
                }
                .padding()
            }
            .onChange(of: model.visibleRows.last?.seq) { _, seq in
                if let seq { withAnimation { proxy.scrollTo(seq, anchor: .bottom) } }
                model.markViewed()
            }
            .onAppear { model.markViewed() }
        }
    }
}

struct Bubble: View {
    let row: Row

    var body: some View {
        let mine = row.role == "user"
        Text(row.content)
            .padding(10)
            .background(mine ? Color.accentColor.opacity(0.85) : Color.secondary.opacity(0.18),
                        in: RoundedRectangle(cornerRadius: 14))
            .foregroundStyle(mine ? .white : .primary)
            .frame(maxWidth: .infinity, alignment: mine ? .trailing : .leading)
            .accessibilityIdentifier("row-\(row.seq)")
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
            HStack {
                TextField(canSend ? "Message Stark" : "Jack In to talk to Stark here",
                          text: $model.draft, axis: .vertical)
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
