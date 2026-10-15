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
    func setDraftProvider(_ provider: @escaping @Sendable () -> String?)
    func confirmDraft(_ handoffId: String)
    func clearSentDraft(_ handoffId: String)
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
    @ObservationIgnored private let store: CredentialStore
    @ObservationIgnored private let makeClient: MakeClient
    @ObservationIgnored private let pairer: Pairer
    @ObservationIgnored private var client: (any PocketClient)?
    @ObservationIgnored private let draftBox = DraftBox()
    /// How long unpair waits for Stark to reach home before it keeps the pairing.
    @ObservationIgnored var unpairTimeout: Duration = .seconds(15)
    @ObservationIgnored private var monitor: NWPathMonitor?
    @ObservationIgnored private var lastPath: NWPath.Status?
    @ObservationIgnored private var foreground = true
    @ObservationIgnored private(set) var viewedSeq = -1

    init(store: CredentialStore = CredentialStore(),
         environment: [String: String] = ProcessInfo.processInfo.environment,
         makeClient: @escaping MakeClient = { GatewayClient(config: $0, onChange: $1) },
         pairer: @escaping Pairer = { try Pairing.run(link: $0, name: $1, signer: $2) }) {
        self.store = store
        self.makeClient = makeClient
        self.pairer = pairer
        stage = PocketAtlas.load().map { PocketStage(atlas: $0) }
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
            pairingError = "Could not reach the Mac. Is stark.py run --lan running on this network?"
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
        client?.stop()
        client = nil
        isPaired = false
        state = nil
        store.wipe()
        viewedSeq = -1
    }

    private func connect(_ endpoint: DeviceEndpoint) {
        client?.stop()                          // never two connections; pair() refuses first
        let cfg = ClientConfig(device: endpoint, backoff: Backoff(base: 0.25, cap: 5),
                               clientName: "StarkPocket/0.4")
        let c = makeClient(cfg, { [weak self] s in
            Task { @MainActor in self?.apply(s) }
        })
        let box = draftBox
        c.setDraftProvider { box.get() }        // a jack out carries the unsent composer text
        client = c
        isPaired = true
        state = ClientState(surface: endpoint.credential.surface)
        viewedSeq = UserDefaults.standard.integer(forKey: "viewedSeq.\(endpoint.credential.agentUUID)") - 1
        watchNetwork()
        if foreground { c.start() }
    }

    private func apply(_ s: ClientState) {
        state = s
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

    func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, summary?.canSend == true else { return }
        client?.send(text, presenceGen: nil)
        draft = ""
    }

    func jack() {
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
            foreground = true
            stage?.setVisible(true)
            client?.start()                     // resyncs rows after the last contiguous seq
        case .background:
            foreground = false
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

    private func watchNetwork() {
        guard monitor == nil else { return }
        let m = NWPathMonitor()
        m.pathUpdateHandler = { [weak self] path in
            Task { @MainActor in
                guard let self else { return }
                let status = path.status
                defer { self.lastPath = status }
                // Back on a network: retry now instead of waiting out the backoff.
                if status == .satisfied, self.lastPath != .satisfied, self.foreground,
                   self.state?.connection.isLive == false {
                    self.client?.reconnectNow()
                }
            }
        }
        m.start(queue: DispatchQueue(label: "stark.pocket.path"))
        monitor = m
    }

    private func followStage() {
        guard let s = state else { return }
        stage?.follow(s, listening: inputFocused, viewedSeq: viewedSeq)
    }
}
