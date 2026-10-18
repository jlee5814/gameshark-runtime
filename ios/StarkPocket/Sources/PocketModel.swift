import Foundation
import Network
import Observation
import StarkKit
import SwiftUI
import UIKit

/// What the model needs from its connection to the runtime. GatewayClient is the only
/// production conformer; unit tests script one. It is a seam, never a second runtime.
protocol PocketClient: AnyObject {
    func start()
    func stop()
    func reconnectNow()
    @discardableResult func send(_ text: String, presenceGen: Int?) -> String
    @discardableResult func jack(_ to: GatewayClient.JackDirection) -> String
    func retryReply(seq: Int)
    func setDraftProvider(_ provider: @escaping @Sendable () -> String?)
    func confirmDraft(_ handoffId: String)
    func clearSentDraft(_ handoffId: String)
    func setPath(_ path: NetworkPath)
    @discardableResult func remember(_ text: String) -> String
    @discardableResult func correctMemory(id: String, revision: Int, text: String) -> String
    @discardableResult func forgetMemory(id: String) -> String
    func discardMemoryOp(_ opId: String)
}

extension GatewayClient: PocketClient {}

/// The phone's whole view of Stark. Identity, presence, duties, and rows come from the
/// runtime through GatewayClient; this object keeps only UI facts (draft, viewed cursor)
/// and the pairing in the Keychain. It never holds agent state of its own.
@MainActor @Observable
final class PocketModel {
    private(set) var state: ClientState?
    private(set) var pairing = false
    private(set) var pairingError: String?
    /// Why the last unpair kept the pairing, with what to do next.
    private(set) var unpairError: String?
    /// Unpair is waiting for Stark to reach home.
    private(set) var unpairing = false
    var draft = "" { didSet { draftBox.set(draft) } }
    /// Unsent text Stark brought from the Mac while this composer held other text.
    private(set) var draftOffer: CarriedDraft?
    var inputFocused = false { didSet { followStage() } }
    var deviceName = UIDevice.current.name

    typealias MakeClient = (ClientConfig, @escaping @Sendable (ClientState) -> Void)
        -> any PocketClient
    typealias Pairer = @Sendable (PairingLink, String, any DeviceSigner) throws
        -> DeviceCredential

    @ObservationIgnored let stage: PocketStage?
    /// Stark's voice on this iPhone: reads new replies aloud while Stark is here. Observable
    /// on its own, so views follow what it is saying.
    @ObservationIgnored let voice: SpeechCoordinator
    @ObservationIgnored private let store: CredentialStore
    @ObservationIgnored private let makeClient: MakeClient
    @ObservationIgnored private let pairer: Pairer
    @ObservationIgnored private var client: (any PocketClient)?
    @ObservationIgnored private let draftBox = DraftBox()
    /// How long unpair waits for Stark to reach home before it keeps the pairing.
    @ObservationIgnored var unpairTimeout: Duration = .seconds(15)
    @ObservationIgnored private var monitor: NWPathMonitor?
    @ObservationIgnored private let monitorsNetwork: Bool
    /// The network class the phone is on, as last reported, so a client woken from the
    /// background learns it before it dials.
    @ObservationIgnored private var network: NetworkPath?
    /// The client is held until NWPathMonitor reports, so its first dial follows the
    /// network: on cellular, the tailnet before any LAN timeout. The fallback for the
    /// current foreground only; cancelled when the path arrives or the app leaves.
    @ObservationIgnored private var firstPathWait: Task<Void, Never>?
    /// How long a client waits for a first path before it dials in the link's order.
    @ObservationIgnored var firstPathTimeout: Duration = .seconds(1)
    /// What Settings shows for diagnosis: the path class and the routes this pairing dials.
    private(set) var networkLabel = "unknown"
    private(set) var routeSummary: [String] = []
    @ObservationIgnored private var foreground = true
    @ObservationIgnored private(set) var viewedSeq = -1

    init(store: CredentialStore = CredentialStore(),
         environment: [String: String] = ProcessInfo.processInfo.environment,
         monitorsNetwork: Bool = true,
         voice: SpeechCoordinator? = nil,
         makeClient: @escaping MakeClient = { GatewayClient(config: $0, onChange: $1) },
         pairer: @escaping Pairer = { try Pairing.run(link: $0, name: $1, signer: $2) }) {
        self.store = store
        self.monitorsNetwork = monitorsNetwork
        self.makeClient = makeClient
        self.pairer = pairer
        stage = PocketAtlas.load().map { PocketStage(atlas: $0) }
        self.voice = voice ?? SpeechCoordinator(engine: AVSpeechEngine(trace: SpeechLog.trace),
                                                store: DefaultsSpeechStore(),
                                                trace: SpeechLog.trace)
        if environment["STARK_RESET"] == "1" { store.wipe() }
        if let endpoint = store.loadDevice() {
            connect(endpoint)
        }
        if let link = environment["STARK_PAIR_LINK"], state == nil {
            Task { await pair(link) }
        }
    }

    /// Observed, unlike client, so the root view switches screens when it changes.
    private(set) var isPaired = false
    var summary: PresenceSummary? { state.map(PresenceSummary.of) }
    var visibleRows: [Row] { state?.rows.filter { $0.role != "system" } ?? [] }
    var pendingOutbox: [OutgoingMessage] {
        state?.outbox.filter { if case .sent = $0.status { false } else { true } } ?? []
    }
    /// What the Mac's runtime is doing with the newest message; nil once Stark answered.
    var replyLine: String? { state?.reply?.caption }
    var canRetryReply: Bool { state?.canRetryReply ?? false }
    /// Set only for the demo model, so a fake answer is never mistaken for Stark's own.
    var modelBadge: String? { state?.model?.fake == true ? "fake model" : nil }

    // MARK: pairing

    /// Run the one time pairing from a starkpocket://pair link (camera scan, paste, or a
    /// test's launch environment), then keep the credential in the Keychain.
    func pair(_ text: String) async {
        // One pairing at a time, and never over a live one: replacing it would orphan a
        // device that may hold Stark. Unpair sends him home first.
        guard !pairing else { return }
        guard !isPaired else {
            pairingError = "This iPhone is already paired with a Mac. Unpair it first; Stark "
                + "goes home before the old pairing is forgotten."
            return
        }
        guard let link = PairingLink(text) else {
            pairingError = "That is not a Stark pairing link."
            return
        }
        pairing = true
        pairingError = nil
        defer { pairing = false }
        let name = deviceName.isEmpty ? "iPhone" : deviceName
        do {
            let (signer, item, blob) = try CredentialStore.newSigner()
            let run = pairer
            let cred = try await Task.detached {
                try run(link, name, signer)
            }.value
            try store.saveDevice(cred, keyItem: item, keyData: blob)
            connect(DeviceEndpoint(credential: cred, signer: signer))
        } catch Pairing.Failure.refused {
            pairingError = "The Mac refused this link. It may be used or expired; run "
                + "stark.py pair again."
        } catch Pairing.Failure.untrusted {
            pairingError = "The Mac's key does not match this link. Pairing stopped."
        } catch Pairing.Failure.unreachable {
            pairingError = "Could not reach the Mac. Is stark.py run --lan running, and is this "
                + "iPhone on the same network or on Tailscale?"
        } catch {
            pairingError = "Pairing failed: \(error)"
        }
    }

    /// Forget this pairing, but never while Stark is here: first send him home and wait
    /// until the runtime confirms the Mac holds him. If that cannot finish (offline, or
    /// the jack stalls), keep the pairing and say how to recover. Returns whether the
    /// pairing was forgotten.
    @discardableResult
    func unpair() async -> Bool {
        guard !unpairing else { return false }
        unpairError = nil
        guard involvesThisPhone else {
            forget()
            return true
        }
        guard let c = client, state?.connection.isLive == true else {
            unpairError = "Stark is on this iPhone. Reconnect to the Mac so he can go home "
                + "first, or Jack In Here on the Mac, then unpair."
            return false
        }
        unpairing = true
        defer { unpairing = false }
        if state?.handoff == nil { c.jack(.home) }
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: unpairTimeout)
        while involvesThisPhone, clock.now < deadline {
            try? await Task.sleep(for: .milliseconds(100))
        }
        guard !involvesThisPhone else {
            unpairError = "Stark could not get home from this iPhone. Your pairing is kept: "
                + "try again, or Jack In Here on the Mac, then unpair."
            return false
        }
        forget()
        return true
    }

    /// This phone holds Stark, an open jack still names it, or nobody has told it yet who
    /// holds him (launched offline): unknown counts as here.
    private var involvesThisPhone: Bool {
        guard let s = state else { return false }
        guard let p = s.presence else { return true }
        if p.surface == s.surface { return true }
        if let h = s.handoff, h.from == s.surface || h.to == s.surface { return true }
        return false
    }

    /// The explicit recovery when Stark cannot get home from here (the Mac is gone, or
    /// its database was reset): forget anyway. The Mac takes him back with Jack In Here,
    /// and `stark.py devices revoke` retires this device there.
    func forgetAnyway() {
        unpairError = nil
        forget()
    }

    /// Drop the connection and the Keychain pairing at once. Only for a pairing that holds
    /// nothing: Stark is elsewhere, or the Mac already revoked this device.
    private func forget() {
        voice.stop()
        client?.stop()
        client = nil
        isPaired = false
        state = nil
        store.wipe()
        viewedSeq = -1
    }

    private func connect(_ endpoint: DeviceEndpoint) {
        client?.stop()                          // never two connections; pair() refuses first
        var cfg = ClientConfig(device: endpoint, backoff: Backoff(base: 0.25, cap: 5),
                               clientName: "StarkPocket/0.4")
        cfg.trace = NetLog.trace
        routeSummary = NetLog.routes(endpoint.credential.routes)
        NetLog.trace("connect build=\(NetLog.build) network=\(network?.rawValue ?? "nil") "
                     + "foreground=\(foreground) routes=\(routeSummary)")
        let c = makeClient(cfg, { [weak self] s in
            Task { @MainActor in self?.apply(s) }
        })
        let box = draftBox
        c.setDraftProvider { box.get() }        // a jack out carries the unsent composer text
        client = c
        if let network { c.setPath(network) }
        isPaired = true
        state = ClientState(surface: endpoint.credential.surface)
        viewedSeq = UserDefaults.standard.integer(forKey: "viewedSeq.\(endpoint.credential.agentUUID)") - 1
        watchNetwork()
        if foreground { startWhenPathKnown() }
    }

    /// Start the client now if the network class is known; otherwise on the first path, or
    /// after firstPathTimeout if the monitor never reports, so the phone never stays offline.
    private func startWhenPathKnown() {
        guard network == nil else {
            client?.start()
            return
        }
        guard firstPathWait == nil else { return }
        let wait = firstPathTimeout
        firstPathWait = Task { @MainActor [weak self] in
            try? await Task.sleep(for: wait)
            guard !Task.isCancelled, let self, self.foreground else { return }
            self.firstPathWait = nil
            NetLog.trace("no network path after \(wait), dialing in the link's order")
            self.client?.start()
        }
    }

    private func apply(_ s: ClientState) {
        state = s
        voice.observe(s)
        if case .disconnected(let why) = s.connection, why == "untrusted runtime" {
            pairingError = s.lastError
        }
        if s.lastError?.contains("auth_failed") == true || s.connection == .disconnected("auth_failed") {
            // Revoked or forgotten by the Mac: this pairing is dead, start over.
            forget()
            pairingError = "This iPhone is no longer paired with the Mac."
            return
        }
        followDrafts()
        followStage()
    }

    /// Restore a carried draft into the composer (never sending it), clear one that left
    /// with Stark, and raise a choice instead of overwriting different text.
    private func followDrafts() {
        guard let s = state, let c = client else { return }
        let steps = DraftContinuity.follow(s, composer: draft, offered: draftOffer)
        if let text = steps.composer { draft = text }
        if let hid = steps.clearSent { c.clearSentDraft(hid) }
        if let hid = steps.confirm { c.confirmDraft(hid) }
        if let offer = steps.offer {
            draftOffer = offer
        } else if steps.withdrawOffer {
            draftOffer = nil
        }
    }

    /// The conflict choice: put the carried text in the composer.
    func useCarriedDraft() {
        guard let offer = draftOffer else { return }
        draft = offer.text
        draftOffer = nil
        client?.confirmDraft(offer.handoffId)
    }

    /// The conflict choice: keep what is here; the runtime stops offering the other text.
    func keepCurrentDraft() {
        guard let offer = draftOffer else { return }
        draftOffer = nil
        client?.confirmDraft(offer.handoffId)
    }

    // MARK: actions

    func retryReply() {
        guard canRetryReply, let seq = state?.reply?.seq else { return }
        client?.retryReply(seq: seq)
    }

    func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        // An explicit command saves memory; it never becomes a chat row.
        switch MemoryCommand.parse(text) {
        case .show?:
            showMemories = true
            draft = ""
            return
        case .remember(let fact)?:
            guard canWriteMemory else { return }
            client?.remember(fact)
            showMemories = true
            draft = ""
            return
        case nil:
            break
        }
        guard summary?.canSend == true else { return }
        voice.userSent()
        client?.send(text, presenceGen: nil)
        draft = ""
    }

    // MARK: memory

    /// The Memories sheet is open.
    var showMemories = false
    var memoryAvailable: Bool { state?.memoryAvailable ?? false }
    var memories: [MemoryItem] { state?.memories ?? [] }
    var memoryOps: [MemoryOp] { state?.memoryOps ?? [] }
    var memoryNotice: String? { state?.memoryNotice }
    var memoryInject: Bool { state?.memoryInject ?? true }
    /// Saving and correcting need Stark here; forgetting only a live connection.
    var canWriteMemory: Bool { state?.canWriteMemory ?? false }
    var canForgetMemory: Bool { state?.canForgetMemory ?? false }

    func addMemory(_ text: String) {
        let fact = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !fact.isEmpty, canWriteMemory else { return }
        client?.remember(fact)
    }

    func correctMemory(_ m: MemoryItem, text: String) {
        let fact = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !fact.isEmpty, fact != m.content, canWriteMemory else { return }
        client?.correctMemory(id: m.id, revision: m.revision, text: fact)
    }

    func forgetMemory(_ id: String) {
        guard canForgetMemory else { return }
        client?.forgetMemory(id: id)
    }

    func dismissMemoryOp(_ id: String) { client?.discardMemoryOp(id) }

    func jack() {
        voice.stop()                            // a jack begins: silence before presence moves
        switch summary?.control {
        case .jackIn: client?.jack(.here)
        case .jackOut: client?.jack(.home)
        default: break
        }
    }

    func markViewed() {
        guard let s = state, let uuid = s.identity?.uuid,
              let newest = s.rows.last(where: { $0.role == "assistant" })?.seq,
              newest > viewedSeq else { return }
        viewedSeq = newest
        UserDefaults.standard.set(newest + 1, forKey: "viewedSeq.\(uuid)")
        followStage()
    }

    // MARK: lifecycle

    /// Foreground: connect and resync. Background: let a pending send finish briefly, then
    /// close cleanly. Presence stays with the phone until an explicit Jack Out or a Jack In
    /// on the Mac; a jack still open when the app leaves reverts through the runtime's
    /// target lost rule. No background modes, no daemon.
    func scenePhase(_ phase: ScenePhase) {
        switch phase {
        case .active:
            NetLog.trace("scene active network=\(network?.rawValue ?? "nil")")
            foreground = true
            voice.setSuspended(false)
            stage?.setVisible(true)
            if let network { client?.setPath(network) }
            startWhenPathKnown()                // resyncs rows after the last contiguous seq
        case .background:
            NetLog.trace("scene background")
            foreground = false
            // No background audio: Stark never talks unseen, even while a send lingers.
            voice.setSuspended(true)
            firstPathWait?.cancel()
            firstPathWait = nil
            stage?.setVisible(false)
            guard let c = client else { return }
            if pendingOutbox.contains(where: { $0.status == .pending }) {
                var task: UIBackgroundTaskIdentifier = .invalid
                task = UIApplication.shared.beginBackgroundTask { c.stop() }
                Task { @MainActor in
                    for _ in 0..<30 where self.pendingOutbox.contains(where: { $0.status == .pending }) {
                        try? await Task.sleep(for: .milliseconds(100))
                    }
                    if !self.foreground { c.stop() }
                    UIApplication.shared.endBackgroundTask(task)
                }
            } else {
                c.stop()
            }
        default:
            break
        }
    }

    /// Wi-Fi, cellular, or offline. The client reorders its routes (LAN first at home,
    /// the Mac's tailnet address away) and redials at once when the class changes, so a
    /// socket left on a vanished interface never waits out a timeout. Background: the
    /// client is stopped; the latest class is handed over when the app returns.
    func networkChanged(_ path: NetworkPath) {
        network = path
        networkLabel = path.rawValue
        guard foreground else { return }
        client?.setPath(path)
        if let wait = firstPathWait {
            wait.cancel()
            firstPathWait = nil
            client?.start()
        }
    }

    private func watchNetwork() {
        guard monitor == nil, monitorsNetwork else { return }
        let m = NWPathMonitor()
        let seen = FirstPath()                  // touched only on the monitor's queue
        m.pathUpdateHandler = { [weak self] path in
            let p = NetworkPath.classify(satisfied: path.status == .satisfied,
                                         wifi: path.usesInterfaceType(.wifi),
                                         wired: path.usesInterfaceType(.wiredEthernet),
                                         cellular: path.usesInterfaceType(.cellular))
            NetLog.trace(NetLog.describe(path, as: p, initial: !seen.value))
            seen.value = true
            Task { @MainActor in self?.networkChanged(p) }
        }
        m.start(queue: DispatchQueue(label: "stark.pocket.path"))
        monitor = m
    }

    private func followStage() {
        guard let s = state else { return }
        stage?.follow(s, listening: inputFocused, viewedSeq: viewedSeq)
    }
}
