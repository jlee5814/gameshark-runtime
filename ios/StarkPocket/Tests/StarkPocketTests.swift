import StarkKit
import XCTest
@testable import StarkPocket

final class CredentialStoreTests: XCTestCase {
    let store = CredentialStore(service: "dev.gameshark.starkpocket.tests")

    override func tearDown() { store.wipe() }

    func testDeviceRoundTripsThroughTheKeychain() throws {
        let (signer, item, blob) = try CredentialStore.newSigner()
        let cred = DeviceCredential(deviceID: String(repeating: "a", count: 32),
                                    surface: "pocket-aaaaaa", name: "Test", hosts: ["127.0.0.1"],
                                    port: 47820, pin: Data(repeating: 7, count: 32),
                                    agentUUID: "uuid")
        try store.saveDevice(cred, keyItem: item, keyData: blob)
        let loaded = try XCTUnwrap(store.loadDevice())
        XCTAssertEqual(loaded.credential, cred)
        XCTAssertEqual(loaded.signer.publicKey, signer.publicKey)    // same key, not a new one
        store.wipe()
        XCTAssertNil(store.loadDevice())
    }

    func testKeyLivesInTheSecureEnclaveWhenTheHardwareHasOne() throws {
        let (_, item, _) = try CredentialStore.newSigner()
        XCTAssertEqual(item, EnclaveSigner.isAvailable ? .enclaveKey : .softwareKey)
    }
}

final class PresenceSummaryTests: XCTestCase {
    func state(holder: String, connected: Bool = true, handoff: HandoffInfo? = nil) -> ClientState {
        var s = ClientState(surface: "pocket-aaaaaa")
        s.connection = connected ? .live : .disconnected("runtime unreachable")
        s.presence = PresenceInfo(surface: holder, home: "mac", generation: 3)
        s.handoff = handoff
        s.deviceNames = ["pocket-aaaaaa": "Jae's iPhone"]
        return s
    }

    func testOffersJackInWhileTheMacHoldsStark() {
        XCTAssertEqual(PresenceSummary.of(state(holder: "mac")),
                       PresenceSummary(label: "Stark is on Mac", control: .jackIn, canSend: false))
    }

    func testOffersJackOutAndSendingWhileHere() {
        XCTAssertEqual(PresenceSummary.of(state(holder: "pocket-aaaaaa")),
                       PresenceSummary(label: "Stark is here", control: .jackOut, canSend: true))
    }

    func testNoControlsDuringAJackOrOffline() {
        let h = HandoffInfo(id: "h", from: "mac", to: "pocket-aaaaaa", state: "preparing",
                            fromGen: 3, toGen: nil)
        XCTAssertEqual(PresenceSummary.of(state(holder: "mac", handoff: h)).control, .none)
        XCTAssertEqual(PresenceSummary.of(state(holder: "pocket-aaaaaa", connected: false)).control,
                       .none)
        XCTAssertFalse(PresenceSummary.of(state(holder: "pocket-aaaaaa", connected: false)).canSend)
    }
}

final class SpriteTests: XCTestCase {
    func testTheBundledAtlasLoads() throws {
        let atlas = try XCTUnwrap(PocketAtlas.load(bundle: Bundle(for: PocketModel.self)))
        XCTAssertEqual(atlas.frames.count, atlas.manifest.frames.count)
    }
}

final class Counter: @unchecked Sendable {
    private let lock = NSLock()
    private var n = 0
    var value: Int { lock.withLock { n } }
    func add() { lock.withLock { n += 1 } }
}

/// A scripted connection: records what the model asks and replays runtime states.
final class FakeClient: PocketClient, @unchecked Sendable {
    let onChange: @Sendable (ClientState) -> Void
    var state: ClientState
    var started = 0
    var stopped = false
    var jacks: [GatewayClient.JackDirection] = []
    var retries: [Int] = []
    var draftProvider: (@Sendable () -> String?)?
    var confirmed: [String] = []
    var clearedSent: [String] = []
    var paths: [NetworkPath] = []
    /// start and setPath in the order the model called them.
    var calls: [String] = []
    /// What the runtime does when this surface asks for a jack; nothing by default.
    var onJack: (FakeClient, GatewayClient.JackDirection) -> Void = { _, _ in }

    init(surface: String, onChange: @escaping @Sendable (ClientState) -> Void) {
        self.onChange = onChange
        state = ClientState(surface: surface)
    }

    func emit(_ change: (inout ClientState) -> Void) {
        change(&state)
        onChange(state)
    }

    func start() {
        started += 1
        calls.append("start")
    }
    func stop() { stopped = true }
    func reconnectNow() {}
    func send(_ text: String, presenceGen: Int?) -> String {
        sent.append(text)
        return "m"
    }
    func jack(_ to: GatewayClient.JackDirection) -> String {
        jacks.append(to)
        onJack(self, to)
        return "h"
    }
    func retryReply(seq: Int) { retries.append(seq) }
    func setDraftProvider(_ provider: @escaping @Sendable () -> String?) { draftProvider = provider }
    func setPath(_ path: NetworkPath) {
        paths.append(path)
        calls.append("path \(path.rawValue)")
    }
    func confirmDraft(_ handoffId: String) {
        confirmed.append(handoffId)
        emit { if $0.incomingDraft?.handoffId == handoffId { $0.incomingDraft = nil } }
    }
    func clearSentDraft(_ handoffId: String) {
        clearedSent.append(handoffId)
        emit { if $0.sentDraft?.handoffId == handoffId { $0.sentDraft = nil } }
    }
    var sent: [String] = []
    var remembered: [String] = []
    var corrected: [String] = []
    var forgotten: [String] = []
    func remember(_ text: String) -> String {
        remembered.append(text)
        return "op"
    }
    func correctMemory(id: String, revision: Int, text: String) -> String {
        corrected.append("\(id)@\(revision):\(text)")
        return "op"
    }
    func forgetMemory(id: String) -> String {
        forgotten.append(id)
        return "op"
    }
    func discardMemoryOp(_ opId: String) {}
}

@MainActor
final class PocketModelTests: XCTestCase {
    let store = CredentialStore(service: "dev.gameshark.starkpocket.modeltests")
    let surface = "pocket-aaaaaa"
    var clients: [FakeClient] = []
    let pairerCalls = Counter()

    override func tearDown() { store.wipe() }

    func credential(_ id: String = String(repeating: "a", count: 32)) -> DeviceCredential {
        DeviceCredential(deviceID: id, surface: surface, name: "Test", hosts: ["127.0.0.1"],
                         port: 47820, pin: Data(repeating: 7, count: 32), agentUUID: "uuid")
    }

    /// A model that finds a stored pairing and connects through a FakeClient.
    func pairedModel() throws -> PocketModel {
        let (_, item, blob) = try CredentialStore.newSigner()
        try store.saveDevice(credential(), keyItem: item, keyData: blob)
        let model = PocketModel(store: store, environment: [:], monitorsNetwork: false,
                                makeClient: { cfg, onChange in
            let c = FakeClient(surface: cfg.surface, onChange: onChange)
            self.clients.append(c)
            return c
        }, pairer: { [calls = pairerCalls, other = credential(String(repeating: "b", count: 32))]
            _, _, _ in
            calls.add()
            return other
        })
        XCTAssertEqual(clients.count, 1)
        return model
    }

    /// Let the model's main actor hops land.
    func settle(until done: @escaping () -> Bool = { false }) async {
        for _ in 0..<50 where !done() {
            try? await Task.sleep(for: .milliseconds(10))
        }
    }

    func holding(_ c: FakeClient, live: Bool = true) {
        c.emit {
            $0.connection = live ? .live : .disconnected("runtime unreachable")
            $0.presence = PresenceInfo(surface: surface, home: "mac", generation: 2)
            $0.handoff = nil
        }
    }

    // Phase 4.2: memory commands are typed, and only an explicit one saves a fact.
    func testRememberCommandSavesMemoryAndNeverSendsChat() async throws {
        let model = try pairedModel()
        let c = clients[0]
        model.networkChanged(.wifi)
        holding(c)
        c.emit { $0.memoryAvailable = true }
        await settle { model.canWriteMemory }
        model.draft = "/remember Parked on level 3"
        model.send()
        XCTAssertEqual(c.remembered, ["Parked on level 3"])
        XCTAssertEqual(c.sent, [])
        XCTAssertTrue(model.showMemories)
        XCTAssertEqual(model.draft, "")
        model.draft = "please remember my locker is 12"
        model.send()
        XCTAssertEqual(c.sent, ["please remember my locker is 12"])   // ordinary chat
        XCTAssertEqual(c.remembered, ["Parked on level 3"])
    }

    func testAGhostPhoneCanForgetButNotSaveOrCorrect() async throws {
        let model = try pairedModel()
        let c = clients[0]
        model.networkChanged(.wifi)
        let m = MemoryItem(id: "mem-1", content: "likes tea", revision: 2, sourceSurface: "mac")
        c.emit {
            $0.connection = .live
            $0.presence = PresenceInfo(surface: "mac", home: "mac", generation: 2)
            $0.memoryAvailable = true
            $0.memories = [m]
        }
        await settle { model.canForgetMemory }
        XCTAssertFalse(model.canWriteMemory)
        model.addMemory("not while Stark is on the Mac")
        model.correctMemory(m, text: "likes coffee")
        model.draft = "/remember nope"
        model.send()
        XCTAssertEqual(c.remembered, [])
        XCTAssertEqual(c.corrected, [])
        model.forgetMemory("mem-1")
        XCTAssertEqual(c.forgotten, ["mem-1"])
    }

    func testACorrectionCarriesTheRevisionItEdited() async throws {
        let model = try pairedModel()
        let c = clients[0]
        model.networkChanged(.wifi)
        holding(c)
        let m = MemoryItem(id: "mem-1", content: "lives in Boston", revision: 4,
                           sourceSurface: "mac")
        c.emit {
            $0.memoryAvailable = true
            $0.memories = [m]
        }
        await settle { model.memories.count == 1 }
        model.correctMemory(m, text: "lives in Boston")             // unchanged: nothing sent
        model.correctMemory(m, text: "  lives in Atlanta ")
        XCTAssertEqual(c.corrected, ["mem-1@4:lives in Atlanta"])
    }

    func testMemoriesStayHiddenFromAnOlderRuntime() async throws {
        let model = try pairedModel()
        let c = clients[0]
        model.networkChanged(.wifi)
        holding(c)
        await settle { model.state?.connection == .live }
        XCTAssertFalse(model.memoryAvailable)
        model.draft = "/remember x"
        model.send()
        XCTAssertEqual(c.remembered, [])
        XCTAssertEqual(c.sent, [])
    }

    // Phase 4.1: the phone shows what the head reply is doing and retries only a failed one.
    func testTheReplyLineFollowsTheRuntimeAndRetryReachesTheClient() async throws {
        let model = try pairedModel()
        let c = clients[0]
        model.networkChanged(.wifi)
        holding(c)
        c.emit { $0.activity = Activity(reply: ReplyState(seq: 3, state: "thinking")) }
        await settle { model.replyLine != nil }
        XCTAssertEqual(model.replyLine, "Stark is thinking…")
        XCTAssertFalse(model.canRetryReply)
        model.retryReply()
        XCTAssertEqual(c.retries, [])                     // nothing failed: nothing to retry
        c.emit { $0.activity = Activity(reply: ReplyState(seq: 3, state: "failed",
                                                          error: "Timeout")) }
        await settle { model.canRetryReply }
        XCTAssertEqual(model.replyLine, "No reply: the model took too long")
        model.retryReply()
        XCTAssertEqual(c.retries, [3])
        c.emit { $0.activity = Activity() }
        await settle { model.replyLine == nil }
        XCTAssertNil(model.replyLine)
    }

    func testAGhostPhoneSeesAFailedReplyButCannotRetryIt() async throws {
        let model = try pairedModel()
        let c = clients[0]
        model.networkChanged(.wifi)
        c.emit {
            $0.connection = .live
            $0.presence = PresenceInfo(surface: "mac", home: "mac", generation: 2)
            $0.activity = Activity(reply: ReplyState(seq: 3, state: "failed", error: "Timeout"))
        }
        await settle { model.replyLine != nil }
        XCTAssertFalse(model.canRetryReply)
        model.retryReply()
        XCTAssertEqual(c.retries, [])
    }

    func testAFakeModelIsLabelled() async throws {
        let model = try pairedModel()
        let c = clients[0]
        model.networkChanged(.wifi)
        c.emit {
            $0.connection = .live
            $0.model = ModelInfo(provider: "companion-fake", model: "companion-fake",
                                 locality: "local", fake: true)
        }
        await settle { model.modelBadge != nil }
        XCTAssertEqual(model.modelBadge, "fake model")
        c.emit { $0.model = ModelInfo(provider: "local", model: "qwen", locality: "local",
                                      fake: false) }
        await settle { model.modelBadge == nil }
        XCTAssertNil(model.modelBadge)
    }

    // Phase 3.3: Wi-Fi and cellular changes reach the connection, which picks the route.
    func testNetworkChangesReachTheClientOnlyInTheForeground() throws {
        let model = try pairedModel()
        let c = clients[0]
        model.networkChanged(.wifi)
        model.networkChanged(.cellular)
        XCTAssertEqual(c.paths, [.wifi, .cellular])
        model.scenePhase(.background)
        model.networkChanged(.wifi)                       // the client is stopped meanwhile
        XCTAssertEqual(c.paths, [.wifi, .cellular])
        model.scenePhase(.active)
        XCTAssertEqual(c.paths.last, .wifi)               // told where it woke up, then started
        XCTAssertEqual(c.started, 2)
    }

    /// Codex P2: NWPathMonitor reports its first path asynchronously. A client started
    /// before it dials in the pairing link's order, LAN first, and on cellular spends the
    /// LAN timeouts before it tries the tailnet.
    func testTheClientWaitsForTheFirstNetworkPathBeforeDialing() throws {
        let model = try pairedModel()
        let c = clients[0]
        XCTAssertEqual(c.started, 0, "the client dialed before the phone knew its network")
        model.networkChanged(.cellular)
        XCTAssertEqual(c.calls, ["path cellular", "start"])
    }

    /// A monitor that never reports must not strand the phone offline.
    func testAPathThatNeverArrivesStillStartsTheClient() async throws {
        let model = try pairedModel()
        let c = clients[0]
        for _ in 0..<200 where c.started == 0 {
            try? await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertEqual(c.started, 1)
        withExtendedLifetime(model) {}                    // the model owns the wait
    }

    /// A wait cut short by the background never starts the client for the next foreground:
    /// only the newest wait may fall back, at its own deadline.
    func testAWaitFromAnEarlierForegroundNeverStartsTheClient() async throws {
        let model = try pairedModel()                     // wait 1 ends at 1.0 s
        let c = clients[0]
        try await Task.sleep(for: .milliseconds(500))
        model.scenePhase(.background)
        model.scenePhase(.active)                         // wait 2 ends at 1.5 s
        try await Task.sleep(for: .milliseconds(700))
        XCTAssertEqual(c.started, 0, "the first wait started the client before the second ended")
        for _ in 0..<100 where c.started == 0 {
            try? await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertEqual(c.started, 1)
    }

    func testInterfacesMapToOneNetworkClass() {
        XCTAssertEqual(NetworkPath.classify(satisfied: false, wifi: true, wired: false,
                                            cellular: false), NetworkPath.none)
        XCTAssertEqual(NetworkPath.classify(satisfied: true, wifi: true, wired: false,
                                            cellular: true), .wifi)
        XCTAssertEqual(NetworkPath.classify(satisfied: true, wifi: false, wired: true,
                                            cellular: false), .wired)
        XCTAssertEqual(NetworkPath.classify(satisfied: true, wifi: false, wired: false,
                                            cellular: true), .cellular)
        XCTAssertEqual(NetworkPath.classify(satisfied: true, wifi: false, wired: false,
                                            cellular: false), .other)
    }

    // Finding 5: a Mac initiated Jack In journals the phone's unsent text.
    func testDraftProviderIsRegisteredAndReadsTheComposer() throws {
        let model = try pairedModel()
        let provider = try XCTUnwrap(clients[0].draftProvider, "no draft provider registered")
        XCTAssertNil(provider())
        model.draft = "half typed"
        XCTAssertEqual(provider(), "half typed")
    }

    // Finding 6: a second pairing never replaces a live connection.
    func testPairingWhilePairedIsRejectedAndKeepsTheLiveClient() async throws {
        let model = try pairedModel()
        let link = "starkpocket://pair?v=1&h=mac.local&a=127.0.0.1&p=47820&fp="
            + String(repeating: "07", count: 32) + "&id=p2&s=secret"
        await model.pair(link)
        XCTAssertEqual(pairerCalls.value, 0)
        XCTAssertEqual(clients.count, 1)
        XCTAssertFalse(clients[0].stopped)
        XCTAssertEqual(store.loadDevice()?.credential, credential())
        XCTAssertTrue(model.pairingError?.contains("already paired") == true)
    }

    // Finding 4: unpair sends Stark home before forgetting the pairing.
    func testUnpairWhileHoldingSendsStarkHomeThenForgets() async throws {
        let model = try pairedModel()
        let c = clients[0]
        holding(c)
        await settle { model.state?.presence?.surface == self.surface }
        c.onJack = { c, _ in
            c.emit { $0.presence = PresenceInfo(surface: "mac", home: "mac", generation: 3) }
        }
        await model.unpair()
        XCTAssertEqual(c.jacks, [.home])
        XCTAssertTrue(c.stopped)
        XCTAssertFalse(model.isPaired)
        XCTAssertNil(store.loadDevice())
    }

    func testUnpairKeepsCredentialsWhenTheHandoffCannotComplete() async throws {
        let model = try pairedModel()
        let c = clients[0]
        model.unpairTimeout = .milliseconds(300)
        holding(c, live: false)                    // offline: nobody can take Stark home
        await settle { model.state?.presence?.surface == self.surface }
        await model.unpair()
        XCTAssertEqual(c.jacks, [])
        XCTAssertTrue(model.isPaired)
        XCTAssertFalse(c.stopped)
        XCTAssertNotNil(store.loadDevice())
        XCTAssertNotNil(model.unpairError)

        holding(c)                                 // live, but the jack never completes
        await settle { model.state?.connection == .live }
        await model.unpair()
        XCTAssertEqual(c.jacks, [.home])
        XCTAssertTrue(model.isPaired)
        XCTAssertFalse(c.stopped)
        XCTAssertNotNil(store.loadDevice())
        XCTAssertNotNil(model.unpairError)
    }

    func testUnpairWhileTheMacHoldsStarkForgetsAtOnce() async throws {
        let model = try pairedModel()
        let c = clients[0]
        c.emit {
            $0.connection = .live
            $0.presence = PresenceInfo(surface: "mac", home: "mac", generation: 1)
        }
        await settle { model.state?.presence != nil }
        await model.unpair()
        XCTAssertEqual(c.jacks, [])
        XCTAssertFalse(model.isPaired)
        XCTAssertNil(store.loadDevice())
    }

    func testUnknownHolderKeepsThePairingUntilForgetAnyway() async throws {
        let model = try pairedModel()                 // launched offline: no snapshot yet
        let c = clients[0]
        let forgotten = await model.unpair()
        XCTAssertFalse(forgotten)
        XCTAssertTrue(model.isPaired)
        XCTAssertNotNil(store.loadDevice())
        XCTAssertNotNil(model.unpairError)
        model.forgetAnyway()                          // the explicit recovery
        XCTAssertTrue(c.stopped)
        XCTAssertFalse(model.isPaired)
        XCTAssertNil(store.loadDevice())
    }

    // Draft continuity: Mac -> iPhone arrival, and iPhone -> Mac departure.
    func testACarriedDraftFillsAnEmptyComposerWithoutSending() async throws {
        let model = try pairedModel()
        let c = clients[0]
        holding(c)
        c.emit { $0.incomingDraft = CarriedDraft(handoffId: "in1", text: "from my mac") }
        await settle { model.draft == "from my mac" }
        XCTAssertEqual(model.draft, "from my mac")
        XCTAssertEqual(c.confirmed, ["in1"])
        XCTAssertNil(model.draftOffer)
        XCTAssertTrue(model.pendingOutbox.isEmpty)               // restored, never sent
    }

    func testACarriedDraftNeverOverwritesOtherText() async throws {
        let model = try pairedModel()
        let c = clients[0]
        holding(c)
        model.draft = "my own words"
        c.emit { $0.incomingDraft = CarriedDraft(handoffId: "in1", text: "from my mac") }
        await settle { model.draftOffer != nil }
        XCTAssertEqual(model.draftOffer?.text, "from my mac")
        XCTAssertEqual(model.draft, "my own words")
        XCTAssertEqual(c.confirmed, [])                          // nothing confirmed yet
        model.useCarriedDraft()
        XCTAssertEqual(model.draft, "from my mac")
        XCTAssertEqual(c.confirmed, ["in1"])
        XCTAssertNil(model.draftOffer)

        model.draft = "edited again"
        c.emit { $0.incomingDraft = CarriedDraft(handoffId: "in2", text: "another") }
        await settle { model.draftOffer != nil }
        model.keepCurrentDraft()
        XCTAssertEqual(model.draft, "edited again")
        XCTAssertEqual(c.confirmed, ["in1", "in2"])
    }

    func testTheComposerClearsOnceStarkLeftWithItsText() async throws {
        let model = try pairedModel()
        let c = clients[0]
        holding(c)
        model.draft = "unsent from my pocket"
        c.emit {
            $0.sentDraft = CarriedDraft(handoffId: "here1", text: "unsent from my pocket")
            $0.handoff = HandoffInfo(id: "here1", from: self.surface, to: "mac",
                                     state: "committed", fromGen: 2, toGen: 3)
            $0.presence = PresenceInfo(surface: "mac", home: "mac", generation: 3)
        }
        await settle()
        XCTAssertEqual(model.draft, "unsent from my pocket")     // jack still open
        c.emit { $0.handoff = nil }
        await settle { model.draft.isEmpty }
        XCTAssertEqual(model.draft, "")
        XCTAssertEqual(c.clearedSent, ["here1"])
    }

    func testRevocationStillForgetsImmediately() async throws {
        let model = try pairedModel()
        let c = clients[0]
        holding(c)
        c.emit {
            $0.connection = .disconnected("auth_failed")
            $0.lastError = "auth_failed: authentication failed"
        }
        await settle { !model.isPaired }
        XCTAssertFalse(model.isPaired)
        XCTAssertNil(store.loadDevice())
        XCTAssertEqual(c.jacks, [])
    }
}
