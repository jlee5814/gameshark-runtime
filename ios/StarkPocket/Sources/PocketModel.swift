import Foundation
import Network
import Observation
import StarkKit
import SwiftUI
import UIKit

/// The phone's whole view of Stark. Identity, presence, duties, and rows come from the
/// runtime through GatewayClient; this object keeps only UI facts (draft, viewed cursor)
/// and the pairing in the Keychain. It never holds agent state of its own.
@MainActor @Observable
final class PocketModel {
    private(set) var state: ClientState?
    private(set) var pairing = false
    private(set) var pairingError: String?
    var draft = ""
    var inputFocused = false { didSet { followStage() } }
    var deviceName = UIDevice.current.name

    @ObservationIgnored let stage: PocketStage?
    @ObservationIgnored private let store: CredentialStore
    @ObservationIgnored private var client: GatewayClient?
    @ObservationIgnored private var monitor: NWPathMonitor?
    @ObservationIgnored private var lastPath: NWPath.Status?
    @ObservationIgnored private var foreground = true
    @ObservationIgnored private(set) var viewedSeq = -1

    init(store: CredentialStore = CredentialStore(),
         environment: [String: String] = ProcessInfo.processInfo.environment) {
        self.store = store
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
            let cred = try await Task.detached {
                try Pairing.run(link: link, name: name, signer: signer)
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

    func unpair() {
        client?.stop()
        client = nil
        isPaired = false
        state = nil
        store.wipe()
        viewedSeq = -1
    }

    private func connect(_ endpoint: DeviceEndpoint) {
        let cfg = ClientConfig(device: endpoint, backoff: Backoff(base: 0.25, cap: 5),
                               clientName: "StarkPocket/0.4")
        let c = GatewayClient(config: cfg, onChange: { [weak self] s in
            Task { @MainActor in self?.apply(s) }
        })
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
            unpair()
            pairingError = "This iPhone is no longer paired with the Mac."
            return
        }
        followStage()
    }

    // MARK: actions

    func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, summary?.canSend == true else { return }
        client?.send(text)
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
