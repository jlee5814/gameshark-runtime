import Observation
import StarkKit
import SwiftUI
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

    /// GatewayClient stamps each publish; so does the fake.
    nonisolated(unsafe) private static var revision = 0

    func emit(_ change: (inout ClientState) -> Void) {
        change(&state)
        Self.revision += 1
        state.revision = Self.revision
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

/// Shared setup for model tests: a stored pairing and a FakeClient per connection.
@MainActor
class PocketHarness: XCTestCase {
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
    func pairedModel(voice: SpeechCoordinator? = nil,
                     listener: (any SpeechInput)? = nil) throws -> PocketModel {
        let (_, item, blob) = try CredentialStore.newSigner()
        try store.saveDevice(credential(), keyItem: item, keyData: blob)
        let model = PocketModel(store: store, environment: [:], monitorsNetwork: false,
                                voice: voice ?? SpeechCoordinator(engine: SilentEngine(),
                                                                  store: MemorySpeechStore()),
                                listener: listener ?? ScriptedInput(),
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

}

@MainActor
final class PocketModelTests: PocketHarness {
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

/// Records speech requests; plays nothing.
@MainActor
final class SilentEngine: SpeechEngine {
    var spoken: [Int] = []
    var playing: Utterance.ID?
    var onFinish: ((Utterance.ID) -> Void)?
    var onInterrupt: (() -> Void)?

    func speak(_ u: Utterance) {
        spoken.append(u.id.seq)
        playing = u.id
    }

    func stop() { playing = nil }
}

/// Phase 4.3B: the phone reads new replies aloud only while Stark is on it, and speech
/// never reaches the runtime.
@MainActor
final class PocketVoiceTests: PocketHarness {
    var engine = SilentEngine()

    func voiced() throws -> (PocketModel, FakeClient) {
        engine = SilentEngine()
        let model = try pairedModel(voice: SpeechCoordinator(engine: engine,
                                                             store: MemorySpeechStore()))
        return (model, clients[0])
    }

    func here(_ c: FakeClient, holder: String? = nil) {
        c.emit {
            $0.connection = .live
            $0.identity = Identity(agentId: "stark", uuid: "uuid")
            $0.presence = PresenceInfo(surface: holder ?? surface, home: "mac", generation: 2)
            $0.handoff = nil
            $0.rows = [Row(seq: 0, role: "system", content: "sys"),
                       Row(seq: 1, role: "user", content: "earlier"),
                       Row(seq: 2, role: "assistant", content: "history")]
        }
    }

    func reply(_ c: FakeClient, _ text: String) {
        c.emit { $0.rows.append(Row(seq: $0.rows.count, role: "assistant", content: text)) }
    }

    func assertNothingSent(_ c: FakeClient, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertTrue(c.sent.isEmpty && c.jacks.isEmpty && c.retries.isEmpty
                      && c.remembered.isEmpty && c.corrected.isEmpty && c.forgotten.isEmpty
                      && c.confirmed.isEmpty, file: file, line: line)
    }

    func testANewReplySpeaksOnceAndHistoryStaysSilent() async throws {
        let (model, c) = try voiced()
        here(c)
        await settle { model.state?.rows.count == 3 }
        XCTAssertEqual(engine.spoken, [])
        reply(c, "Jacked in and ready!")
        await settle { self.engine.spoken.count == 1 }
        XCTAssertEqual(engine.spoken, [3])
        XCTAssertEqual(model.voice.speakingSeq, 3)
        c.emit { _ in }                                  // republished, nothing new
        await settle()
        XCTAssertEqual(engine.spoken, [3])
        assertNothingSent(c)
    }

    func testAGhostPhoneStaysSilent() async throws {
        let (model, c) = try voiced()
        here(c, holder: "mac")
        reply(c, "for the Mac")
        await settle { model.state?.rows.count == 4 }
        XCTAssertEqual(engine.spoken, [])
        XCTAssertFalse(model.voice.canReplay)
    }

    func testSendingAMessageStopsSpeech() async throws {
        let (model, c) = try voiced()
        here(c)
        reply(c, "a long answer")
        await settle { self.engine.playing != nil }
        model.draft = "wait, one more thing"
        model.send()
        XCTAssertNil(engine.playing)
        XCTAssertEqual(c.sent, ["wait, one more thing"])
    }

    func testJackOutStopsSpeech() async throws {
        let (model, c) = try voiced()
        here(c)
        reply(c, "a long answer")
        await settle { self.engine.playing != nil }
        model.jack()
        XCTAssertNil(engine.playing)
        XCTAssertEqual(c.jacks, [.home])
    }

    func testLeavingTheForegroundStopsSpeech() async throws {
        let (model, c) = try voiced()
        here(c)
        reply(c, "a long answer")
        await settle { self.engine.playing != nil }
        model.scenePhase(.background)
        XCTAssertNil(engine.playing)
    }

    func testAReplyDuringTheBackgroundGraceStaysSilent() async throws {
        let (model, c) = try voiced()
        here(c)
        await settle { model.state?.rows.count == 3 }
        c.emit { $0.outbox = [OutgoingMessage(id: "m", text: "bye", status: .pending)] }
        await settle { model.pendingOutbox.count == 1 }
        model.scenePhase(.background)                   // the client lingers for the send
        c.emit { s in
            s.outbox = []
            s.rows.append(Row(seq: 3, role: "user", content: "bye"))
            s.rows.append(Row(seq: 4, role: "assistant", content: "see you"))
        }
        await settle { model.state?.rows.count == 5 }
        XCTAssertEqual(engine.spoken, [])
        model.scenePhase(.active)
        c.emit { _ in }
        await settle()
        XCTAssertEqual(engine.spoken, [])
    }

    func testReplayAndMuteStayOnThePhone() async throws {
        let (model, c) = try voiced()
        here(c)
        await settle { model.state?.rows.count == 3 }
        model.voice.replay(seq: 2)
        XCTAssertEqual(engine.spoken, [2])
        model.voice.setMuted(true)
        reply(c, "muted")
        await settle { model.state?.rows.count == 4 }
        model.voice.setMuted(false)
        await settle()
        XCTAssertEqual(engine.spoken, [2])
        assertNothingSent(c)
    }
}


/// Set when an observed property changes; the change handler may run off the main actor.
final class ObservedChange: @unchecked Sendable {
    var on = false
}

/// A recognizer the test speaks through; never touches the microphone.
@MainActor
final class ScriptedInput: SpeechInput {
    var availability: InputAvailability = .ready
    var availabilityChanged: (@MainActor () -> Void)?
    var starts = 0
    var cancels = 0
    private var sink: (@MainActor (Int, InputEvent) -> Void)?
    private var session = 0

    func requestPermission(_ done: @escaping @MainActor (InputAvailability) -> Void) {
        done(availability)
    }
    func start(session: Int, _ events: @escaping @MainActor (Int, InputEvent) -> Void) {
        starts += 1
        self.session = session
        sink = events
    }
    func finish() {}
    func cancel() { cancels += 1 }
    func hear(_ event: InputEvent) { sink?(session, event) }
}

/// Phase 4.3C: push to talk on the phone. The voice draft stays on this iPhone, nothing
/// reaches the runtime until Send, and the ordinary composer keeps its continuity.
@MainActor
final class PocketVoiceInputTests: PocketHarness {
    var input = ScriptedInput()

    func listening() throws -> (PocketModel, FakeClient) {
        input = ScriptedInput()
        let model = try pairedModel(listener: input)
        let c = clients[0]
        c.emit {
            $0.connection = .live
            $0.identity = Identity(agentId: "stark", uuid: "uuid")
            $0.presence = PresenceInfo(surface: surface, home: "mac", generation: 2)
            $0.handoff = nil
            $0.rows = [Row(seq: 0, role: "system", content: "sys")]
        }
        return (model, c)
    }

    func dictate(_ model: PocketModel, _ text: String) {
        model.listen.press()
        input.hear(.partial(text))
        model.listen.release()
        input.hear(.final(text))
    }

    func assertNothingSent(_ c: FakeClient, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertTrue(c.sent.isEmpty && c.jacks.isEmpty && c.retries.isEmpty
                      && c.remembered.isEmpty && c.corrected.isEmpty && c.forgotten.isEmpty
                      && c.confirmed.isEmpty && c.clearedSent.isEmpty, file: file, line: line)
    }

    func testCapturingAndEditingSendNothingToTheRuntime() async throws {
        let (model, c) = try listening()
        await settle { model.listen.isPresent }
        model.listen.press()
        XCTAssertEqual(input.starts, 1)
        input.hear(.partial("what is on"))
        model.listen.release()
        input.hear(.final("What is on my list?"))
        model.listen.edit("What is on my list today?")
        XCTAssertEqual(model.listen.phase, .reviewing)
        assertNothingSent(c)
    }

    /// A final equal to the last partial changes no text; the card must still leave
    /// Listening for the editable draft, which a view sees only through observation.
    func testReleaseShowsTheVoiceDraftWhenTheFinalMatchesThePartial() async throws {
        let (model, c) = try listening()
        await settle { model.listen.isPresent }
        model.listen.press()
        input.hear(.partial("Testing microphone release"))
        model.listen.release()
        XCTAssertEqual(model.listen.panel, .finishing)
        let fired = ObservedChange()
        withObservationTracking { _ = model.listen.isListening } onChange: { fired.on = true }
        input.hear(.final("Testing microphone release"))
        XCTAssertTrue(fired.on)
        XCTAssertEqual(model.listen.panel, .reviewing)
        XCTAssertEqual(model.listen.draft, "Testing microphone release")
        assertNothingSent(c)
    }

    /// The same finishing sequence the Mac runs: release turns the red microphone off at
    /// once, finishing never reads as recording, a press meanwhile starts nothing, and the
    /// delayed final still opens the editable draft.
    func testReleaseLeavesRecordingAtOnceAndFinishingIgnoresPresses() async throws {
        let (model, c) = try listening()
        await settle { model.listen.isPresent }
        XCTAssertEqual(model.listen.mic, .ready)
        model.listen.press()
        XCTAssertEqual(model.listen.mic, .recording)
        XCTAssertEqual(model.listen.panel, .listening)
        input.hear(.partial("short"))
        let fired = ObservedChange()
        withObservationTracking { _ = model.listen.mic } onChange: { fired.on = true }
        model.listen.release()
        XCTAssertTrue(fired.on)
        XCTAssertEqual(model.listen.mic, .finishing)
        XCTAssertFalse(model.listen.isRecording)
        XCTAssertEqual(model.listen.panel, .finishing)
        model.listen.press()
        model.listen.release()
        XCTAssertEqual(input.starts, 1)
        input.hear(.partial("short sentence"))
        XCTAssertEqual(model.listen.mic, .finishing)
        input.hear(.final("Short sentence."))
        XCTAssertEqual(model.listen.panel, .reviewing)
        XCTAssertEqual(model.listen.mic, .ready)
        XCTAssertEqual(model.listen.draft, "Short sentence.")
        assertNothingSent(c)
    }

    func testCancelWhileFinishingDiscardsTheDraft() async throws {
        let (model, c) = try listening()
        await settle { model.listen.isPresent }
        model.listen.press()
        input.hear(.partial("never mind"))
        model.listen.release()
        model.listen.cancel(.user)
        XCTAssertEqual(model.listen.phase, .idle)
        XCTAssertEqual(model.listen.draft, "")
        XCTAssertEqual(input.cancels, 1)
        input.hear(.final("never mind"))
        XCTAssertEqual(model.listen.panel, .none)
        XCTAssertEqual(model.listen.draft, "")
        assertNothingSent(c)
    }

    func testSendSubmitsOnceAndLeavesTheComposerAlone() async throws {
        let (model, c) = try listening()
        await settle { model.listen.isPresent }
        model.draft = "typed, not sent"
        dictate(model, "Spoken words")
        model.listen.send()
        model.listen.send()
        XCTAssertEqual(c.sent, ["Spoken words"])
        XCTAssertEqual(model.draft, "typed, not sent")
        XCTAssertEqual(model.listen.phase, .idle)
    }

    func testTheVoiceDraftNeverTravelsAndInsertJoinsTheComposer() async throws {
        let (model, c) = try listening()
        await settle { model.listen.isPresent }
        dictate(model, "remind me later")
        // A jack out carries the composer, never the voice draft.
        XCTAssertNil(c.draftProvider?())
        model.draft = "Note:"
        XCTAssertEqual(c.draftProvider?(), "Note:")
        model.listen.insertIntoComposer()
        XCTAssertEqual(model.draft, "Note: remind me later")
        XCTAssertEqual(c.draftProvider?(), "Note: remind me later")
        assertNothingSent(c)
    }

    func testACarriedDraftStillArrivesInTheComposer() async throws {
        let (model, c) = try listening()
        await settle { model.listen.isPresent }
        dictate(model, "voice text")
        c.emit { $0.incomingDraft = CarriedDraft(handoffId: "h9", text: "from the Mac") }
        await settle { model.draft == "from the Mac" }
        XCTAssertEqual(model.draft, "from the Mac")
        XCTAssertEqual(model.listen.draft, "voice text")
    }

    func testBackgroundDiscardsTheVoiceDraft() async throws {
        let (model, _) = try listening()
        await settle { model.listen.isPresent }
        model.listen.press()
        input.hear(.partial("half a thought"))
        model.scenePhase(.background)
        XCTAssertEqual(model.listen.phase, .idle)
        XCTAssertEqual(model.listen.draft, "")
        XCTAssertEqual(input.cancels, 1)
    }

    func testInactiveEndsAHoldButKeepsAReviewedDraft() async throws {
        let (model, _) = try listening()
        await settle { model.listen.isPresent }
        model.listen.press()
        input.hear(.partial("held"))
        model.scenePhase(.inactive)
        XCTAssertEqual(model.listen.phase, .idle)
        XCTAssertEqual(input.cancels, 1)
        dictate(model, "keep me")
        model.scenePhase(.inactive)
        XCTAssertEqual(model.listen.phase, .reviewing)
        XCTAssertEqual(model.listen.draft, "keep me")
    }

    func testJackOutAndAPresenceChangeDiscard() async throws {
        let (model, c) = try listening()
        await settle { model.listen.isPresent }
        dictate(model, "first")
        model.jack()
        XCTAssertEqual(model.listen.phase, .idle)
        c.emit { $0.handoff = nil }
        dictate(model, "second")
        c.emit { $0.presence = PresenceInfo(surface: "mac", home: "mac", generation: 3) }
        await settle { model.listen.phase == .idle }
        XCTAssertEqual(model.listen.draft, "")
        XCTAssertFalse(model.listen.canPress)
    }

    func testForgettingThePairingDisablesListening() async throws {
        let (model, _) = try listening()
        await settle { model.listen.isPresent }
        dictate(model, "left behind")
        model.forgetAnyway()
        XCTAssertFalse(model.listen.isPresent)
        XCTAssertFalse(model.listen.canPress)
        XCTAssertEqual(model.listen.draft, "")
        model.listen.press()
        XCTAssertEqual(input.starts, 1)
    }

    func testAPhoneWithoutStarkCannotListen() async throws {
        let (model, c) = try listening()
        c.emit { $0.presence = PresenceInfo(surface: "mac", home: "mac", generation: 3) }
        await settle { model.state?.presence?.surface == "mac" }
        model.listen.press()
        XCTAssertEqual(input.starts, 0)
        assertNothingSent(c)
    }

    func testSpeakingStopsWhenThePhoneStartsListening() async throws {
        let engine = SilentEngine()
        input = ScriptedInput()
        let model = try pairedModel(voice: SpeechCoordinator(engine: engine,
                                                             store: MemorySpeechStore()),
                                    listener: input)
        let c = clients[0]
        c.emit {
            $0.connection = .live
            $0.identity = Identity(agentId: "stark", uuid: "uuid")
            $0.presence = PresenceInfo(surface: surface, home: "mac", generation: 2)
            $0.rows = [Row(seq: 0, role: "system", content: "sys")]
        }
        await settle { model.listen.isPresent }
        c.emit { $0.rows.append(Row(seq: 1, role: "assistant", content: "talking")) }
        await settle { engine.playing != nil }
        model.listen.press()
        XCTAssertNil(engine.playing)
        c.emit { $0.rows.append(Row(seq: 2, role: "assistant", content: "while listening")) }
        await settle { model.state?.rows.count == 3 }
        XCTAssertEqual(engine.spoken, [1])
    }
}

/// The reviewed voice draft on screen: SwiftUI hosted in a window the size of an iPhone 18
/// Pro, with the keyboard and its QuickType bar simulated by the height they take away.
@MainActor
final class PocketVoiceDraftLayoutTests: PocketHarness {
    var input = ScriptedInput()
    let transcript = "Stark, remind me to review the pull request after lunch, then check "
        + "whether the continuous integration run passed, and finally draft a note to the "
        + "team about the release schedule for next week."

    func reviewing() async throws -> (PocketModel, FakeClient, UIWindow) {
        input = ScriptedInput()
        let model = try pairedModel(listener: input)
        let c = clients[0]
        c.emit {
            $0.connection = .live
            $0.identity = Identity(agentId: "stark", uuid: "uuid")
            $0.presence = PresenceInfo(surface: surface, home: "mac", generation: 2)
            $0.handoff = nil
            $0.rows = [Row(seq: 0, role: "system", content: "sys")]
        }
        await settle { model.listen.isPresent }
        model.listen.press()
        input.hear(.partial(transcript))
        model.listen.release()
        input.hear(.final(transcript))
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 402, height: 874))
        window.rootViewController = UIHostingController(rootView: MainView(model: model))
        window.makeKeyAndVisible()
        await layout(window)
        return (model, c, window)
    }

    func layout(_ window: UIWindow) async {
        window.layoutIfNeeded()
        await settle()
    }

    func textViews(in view: UIView) -> [UITextView] {
        (view as? UITextView).map { [$0] } ?? view.subviews.flatMap { textViews(in: $0) }
    }

    /// The voice card sits above the composer: the first field is the voice draft.
    func fields(_ window: UIWindow) throws -> (voice: UITextView, composer: UITextView) {
        let all = textViews(in: window)
        XCTAssertEqual(all.count, 2)
        return (try XCTUnwrap(all.first), try XCTUnwrap(all.last))
    }

    /// The keyboard squeezed the vertical voice field to zero height (a thin white strip)
    /// while the text stayed in the draft; both fields now keep their lines.
    func testTheVoiceDraftStaysLegibleWhileTheKeyboardIsUp() async throws {
        let (model, c, window) = try await reviewing()
        let open = try fields(window)
        XCTAssertGreaterThan(open.voice.frame.height, 60)
        open.voice.becomeFirstResponder()
        window.frame.size.height = 874 - 380
        await layout(window)
        let squeezed = try fields(window)
        XCTAssertTrue(squeezed.voice === open.voice)
        XCTAssertGreaterThan(squeezed.voice.frame.height, 60)
        XCTAssertGreaterThan(squeezed.composer.frame.height, 20)
        XCTAssertLessThanOrEqual(squeezed.composer.convert(squeezed.composer.bounds, to: nil).maxY,
                                 window.bounds.maxY)
        squeezed.voice.insertText(" Thanks")
        await settle()
        XCTAssertEqual(model.listen.draft, transcript + " Thanks")
        XCTAssertEqual(squeezed.voice.text, model.listen.draft)
        squeezed.voice.resignFirstResponder()
        window.frame.size.height = 874
        await layout(window)
        let closed = try fields(window)
        XCTAssertGreaterThan(closed.voice.frame.height, 60)
        XCTAssertEqual(closed.voice.text, transcript + " Thanks")
        XCTAssertEqual(model.listen.phase, .reviewing)
        XCTAssertTrue(c.sent.isEmpty)
    }

    func testAnEditReachesSendAndInsert() async throws {
        let (model, c, window) = try await reviewing()
        try fields(window).voice.insertText(" Thanks")
        await settle()
        XCTAssertTrue(c.sent.isEmpty)
        model.listen.send()
        XCTAssertEqual(c.sent, [transcript + " Thanks"])
        XCTAssertEqual(model.draft, "")
        model.listen.press()
        input.hear(.final("Second take"))
        await layout(window)
        try fields(window).voice.insertText(", edited")
        await settle()
        model.listen.insertIntoComposer()
        XCTAssertEqual(model.draft, "Second take, edited")
        XCTAssertEqual(c.sent, [transcript + " Thanks"])
    }
}

/// Phase 4.4A: the header collapses to one row while a software keyboard is up. MainView is
/// hosted in a window the size of each iPhone class; the keyboard is a posted notification
/// plus the height it takes away, as PocketVoiceDraftLayoutTests simulates it.
@MainActor
final class PocketConversationLayoutTests: PocketHarness {
    /// iPhone SE class (the smallest height), 17e, 18 Pro, 18 Pro Max.
    let screens: [CGSize] = [CGSize(width: 375, height: 667), CGSize(width: 390, height: 844),
                             CGSize(width: 402, height: 874), CGSize(width: 440, height: 956)]
    var center = NotificationCenter()
    var keyboard: KeyboardObserver!
    var input = ScriptedInput()
    var windows: [UIWindow] = []

    func row(_ seq: Int) -> Row {
        Row(seq: seq, role: seq % 2 == 1 ? "user" : "assistant",
            content: "Message \(seq): a line long enough to wrap onto a second line on the "
                + "narrowest iPhone.")
    }

    /// Stark is here and the runtime has `count` rows of conversation.
    func conversation(_ count: Int) throws -> (PocketModel, FakeClient) {
        store.wipe()
        clients = []
        input = ScriptedInput()
        let model = try pairedModel(listener: input)
        let c = clients[0]
        c.emit {
            $0.connection = .live
            $0.identity = Identity(agentId: "stark", uuid: "uuid")
            $0.presence = PresenceInfo(surface: surface, home: "mac", generation: 2)
            $0.handoff = nil
            $0.rows = [Row(seq: 0, role: "system", content: "sys")] + (1...count).map(row)
        }
        return (model, c)
    }

    /// MainView in a window of the host app's scene, so SwiftUI keeps rendering changes.
    /// The newest row then arrives while the view is up and scrolls into view, as it does in
    /// the app; without it the list stays at the oldest rows, as if reading history.
    /// With `realKeyboard`, the header follows the system's own keyboard notifications.
    func host(_ model: PocketModel, _ c: FakeClient, _ size: CGSize,
              arrive: Bool = true, realKeyboard: Bool = false) async -> UIWindow {
        await settle { !model.visibleRows.isEmpty && model.summary?.canSend == true }
        // Not in tearDown: on Xcode 16.4 it is nonisolated and cannot touch a window.
        windows.forEach { $0.isHidden = true; $0.rootViewController = nil }
        windows = []
        center = NotificationCenter()
        keyboard = realKeyboard ? KeyboardObserver()
                                : KeyboardObserver(center: center, screenHeight: { _ in size.height })
        let scene = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first
        let window = scene.map(UIWindow.init(windowScene:)) ?? UIWindow()
        window.frame = CGRect(origin: .zero, size: size)
        window.rootViewController = UIHostingController(rootView: MainView(model: model,
                                                                           keyboard: keyboard))
        window.makeKeyAndVisible()
        windows.append(window)
        await layout(window)
        if arrive {
            let next = model.visibleRows.count + 1
            c.emit { $0.rows.append(row(next)) }
            await layout(window)
        }
        return window
    }

    func layout(_ window: UIWindow) async {
        window.layoutIfNeeded()
        await settle()
        window.layoutIfNeeded()
    }

    func openKeyboard(_ height: CGFloat, screen: CGSize, _ window: UIWindow) async {
        center.post(name: UIResponder.keyboardWillChangeFrameNotification, object: nil, userInfo: [
            UIResponder.keyboardFrameEndUserInfoKey: CGRect(x: 0, y: screen.height - height,
                                                            width: screen.width, height: height),
            UIResponder.keyboardAnimationDurationUserInfoKey: 0.0,
        ])
        window.frame.size.height = screen.height - height
        await layout(window)
    }

    func closeKeyboard(screen: CGSize, _ window: UIWindow) async {
        center.post(name: UIResponder.keyboardWillHideNotification, object: nil, userInfo: [
            UIResponder.keyboardFrameEndUserInfoKey: CGRect(x: 0, y: screen.height,
                                                            width: screen.width, height: 336),
        ])
        window.frame.size.height = screen.height
        await layout(window)
    }

    /// The simulator acts as if a hardware keyboard is attached, so the software keyboard
    /// never rises. Turning UIKit's hardware keyboard mode off (a test only private switch)
    /// brings up the real keyboard with its real frames, animation, and safe area.
    func softwareKeyboard(_ on: Bool) throws {
        // Fail, never skip: a silent skip would hide lost real keyboard coverage.
        struct Unavailable: Error {}
        guard let type = NSClassFromString("UIKeyboardImpl") as? NSObject.Type,
              let impl = type.perform(NSSelectorFromString("sharedInstance"))?
                .takeUnretainedValue() as? NSObject,
              impl.responds(to: NSSelectorFromString("setInHardwareKeyboardMode:")),
              impl.responds(to: NSSelectorFromString("setAutomaticMinimizationEnabled:")) else {
            XCTFail("UIKeyboardImpl's hardware keyboard switch is unavailable: real keyboard "
                    + "coverage lost")
            throw Unavailable()
        }
        impl.setValue(!on, forKey: "automaticMinimizationEnabled")
        impl.setValue(!on, forKey: "inHardwareKeyboardMode")
    }

    /// Waits for the real keyboard's animation to finish and the header to follow it. Fails
    /// if it never does, so a keyboard that never rose cannot pass a test vacuously.
    func waitForHeader(_ mode: HeaderMode, _ window: UIWindow,
                       file: StaticString = #filePath, line: UInt = #line) async {
        for _ in 0..<30 where keyboard.mode != mode {
            try? await Task.sleep(for: .milliseconds(100))
            window.layoutIfNeeded()
        }
        XCTAssertEqual(keyboard.mode, mode, "the software keyboard did not "
                       + (mode == .compact ? "appear" : "leave"), file: file, line: line)
        try? await Task.sleep(for: .milliseconds(400))    // the keyboard animation's tail
        window.layoutIfNeeded()
    }

    func views<T: UIView>(_ type: T.Type, in view: UIView) -> [T] {
        ((view as? T).map { [$0] } ?? []) + view.subviews.flatMap { views(type, in: $0) }
    }

    /// The conversation: the one scroll view that is not a text field.
    func conversationView(_ window: UIWindow) throws -> UIScrollView {
        try XCTUnwrap(views(UIScrollView.self, in: window).first { !($0 is UITextView) })
    }

    func frame(_ view: UIView) -> CGRect { view.convert(view.bounds, to: nil) }

    func metrics(_ list: UIScrollView) -> String {
        "offset \(list.contentOffset.y) height \(list.bounds.height) content \(list.contentSize.height)"
    }

    func atNewest(_ list: UIScrollView) -> Bool {
        // The newest bubble is in view. Below it sits the stack's padding (16); scrollTo in
        // a lazy stack also lands on an estimated row height, so allow one text line more.
        list.contentOffset.y + list.bounds.height - list.adjustedContentInset.bottom
            >= list.contentSize.height - 40
    }

    /// Blank space below the content: the visible bottom past the content's end.
    func gap(_ list: UIScrollView) -> CGFloat {
        list.contentOffset.y + list.bounds.height - list.adjustedContentInset.bottom
            - list.contentSize.height
    }

    func snapshot(_ window: UIWindow, _ name: String) {
        let image = UIGraphicsImageRenderer(bounds: window.bounds).image { ctx in
            window.layer.render(in: ctx.cgContext)
        }
        let a = XCTAttachment(image: image)
        a.name = name
        a.lifetime = .keepAlways
        add(a)
    }

    func testOnlyASoftwareKeyboardCollapsesTheHeader() {
        XCTAssertEqual(KeyboardLayout.mode(overlap: 0), .full)
        XCTAssertEqual(KeyboardLayout.mode(overlap: 55), .full)      // hardware shortcut bar
        XCTAssertEqual(KeyboardLayout.mode(overlap: 119), .full)
        XCTAssertEqual(KeyboardLayout.mode(overlap: 120), .compact)
        XCTAssertEqual(KeyboardLayout.mode(overlap: 336), .compact)
        let gone = CGRect(x: 0, y: 874, width: 402, height: 336)
        XCTAssertEqual(KeyboardLayout.overlap(of: gone, screenHeight: 874), 0)
        XCTAssertEqual(KeyboardLayout.overlap(of: .zero, screenHeight: 874), 0)
        XCTAssertEqual(KeyboardLayout.overlap(of: CGRect(x: 0, y: 538, width: 402, height: 336),
                                              screenHeight: 874), 336)
    }

    func testTheObserverFollowsTheKeyboardFrame() {
        let center = NotificationCenter()
        let keyboard = KeyboardObserver(center: center, screenHeight: { _ in 874 })
        func change(_ height: CGFloat) {
            center.post(name: UIResponder.keyboardWillChangeFrameNotification, object: nil,
                        userInfo: [UIResponder.keyboardFrameEndUserInfoKey:
                                    CGRect(x: 0, y: 874 - height, width: 402, height: height)])
        }
        XCTAssertEqual(keyboard.mode, .full)
        change(336)
        XCTAssertEqual(keyboard.mode, .compact)
        change(380)                                     // QuickType bar appears: still compact
        XCTAssertEqual(keyboard.mode, .compact)
        change(55)                                      // a hardware keyboard takes over
        XCTAssertEqual(keyboard.mode, .full)
        change(336)
        center.post(name: UIResponder.keyboardWillHideNotification, object: nil, userInfo: [:])
        XCTAssertEqual(keyboard.mode, .full)
    }

    func testTheHeaderCollapsesWhileTheKeyboardIsUpOnEveryScreen() async throws {
        // The SE class keyboard with QuickType is 260 points; Face ID iPhones add more.
        let cases: [(CGSize, CGFloat)] = [(screens[0], 260), (screens[0], 216), (screens[1], 336),
                                          (screens[2], 336), (screens[2], 291), (screens[3], 346)]
        for (screen, keyboardHeight) in cases {
            let (model, c) = try conversation(12)
            model.draft = "Half typed"
            let window = await host(model, c, screen)
            let tag = "\(Int(screen.width))x\(Int(screen.height)) keyboard \(Int(keyboardHeight))"
            let closed = try conversationView(window)
            let closedTop = frame(closed).minY
            XCTAssertEqual(keyboard.mode, .full, tag)
            XCTAssertTrue(atNewest(closed), "\(tag) \(metrics(closed))")
            snapshot(window, "\(tag) closed")

            await openKeyboard(keyboardHeight, screen: screen, window)
            let open = try conversationView(window)
            XCTAssertEqual(keyboard.mode, .compact, tag)
            // The header gave up at least 90 points to the conversation.
            XCTAssertLessThanOrEqual(frame(open).minY, closedTop - 90, tag)
            XCTAssertGreaterThan(open.bounds.height, 90, tag)
            XCTAssertTrue(atNewest(open), "\(tag) \(metrics(open))")
            let composer = try XCTUnwrap(views(UITextView.self, in: window).last)
            XCTAssertGreaterThan(composer.bounds.height, 20, tag)
            XCTAssertGreaterThanOrEqual(frame(composer).minY, frame(open).maxY, tag)
            XCTAssertLessThanOrEqual(frame(composer).maxY, window.bounds.maxY, tag)
            snapshot(window, "\(tag) open")

            await closeKeyboard(screen: screen, window)
            XCTAssertEqual(keyboard.mode, .full, tag)
            XCTAssertEqual(frame(try conversationView(window)).minY, closedTop, accuracy: 1, tag)
            XCTAssertEqual(model.draft, "Half typed", tag)
            XCTAssertTrue(c.sent.isEmpty, tag)
            XCTAssertTrue(c.jacks.isEmpty, tag)
            window.isHidden = true
        }
    }

    func testReadingHistoryKeepsItsPlaceWhenTheKeyboardOpens() async throws {
        let screen = screens[0]
        let (model, c) = try conversation(40)
        let window = await host(model, c, screen, arrive: false)
        let list = try conversationView(window)
        let top = list.contentOffset.y
        XCTAssertFalse(atNewest(list), metrics(list))
        await openKeyboard(260, screen: screen, window)
        let open = try conversationView(window)
        XCTAssertEqual(keyboard.mode, .compact)
        XCTAssertEqual(open.contentOffset.y, top, accuracy: 1, metrics(open))
        XCTAssertFalse(atNewest(open), metrics(open))
        // A new reply still brings the newest message into view with the keyboard up.
        c.emit { $0.rows.append(self.row(41)) }
        await layout(window)
        XCTAssertTrue(atNewest(open), metrics(open))
        await closeKeyboard(screen: screen, window)
        XCTAssertEqual(keyboard.mode, .full)
        XCTAssertTrue(atNewest(try conversationView(window)))
    }

    /// With the real keyboard: a reply that arrives while it is up stays anchored at the
    /// bottom when it leaves, and again when it returns. An animated header resize used to
    /// override the anchoring scroll and leave the newest reply under the edge.
    func testTheRealKeyboardLeavesTheNewestReplyAnchored() async throws {
        let (model, c) = try conversation(20)
        model.draft = "Half typed"
        let window = await host(model, c, UIScreen.main.bounds.size, realKeyboard: true)
        try softwareKeyboard(true)
        defer { try? softwareKeyboard(false) }
        func check(_ step: String, _ mode: HeaderMode) throws {
            let list = try conversationView(window)
            XCTAssertEqual(keyboard.mode, mode, step)
            XCTAssertTrue(atNewest(list), "\(step) \(metrics(list))")
            XCTAssertLessThanOrEqual(gap(list), 2, "\(step) gap \(gap(list))")
        }
        try check("hosted", .full)
        let composer = try XCTUnwrap(views(UITextView.self, in: window).last)
        XCTAssertTrue(composer.becomeFirstResponder())
        await waitForHeader(.compact, window)
        try check("open", .compact)
        c.emit { $0.rows.append(self.row(22)) }
        await waitForHeader(.compact, window)
        try check("reply with the keyboard up", .compact)
        KeyboardLayout.dismiss()
        await waitForHeader(.full, window)
        try check("dismissed", .full)
        XCTAssertTrue(composer.becomeFirstResponder())
        await waitForHeader(.compact, window)
        try check("reopened", .compact)
        KeyboardLayout.dismiss()
        await waitForHeader(.full, window)
        XCTAssertEqual(model.draft, "Half typed")
        XCTAssertTrue(c.sent.isEmpty)
        XCTAssertTrue(c.jacks.isEmpty)
    }

    /// With the real keyboard: someone reading older history keeps their place as it opens
    /// and closes, twice, and no blank space opens under the conversation.
    func testTheRealKeyboardKeepsAReadersPlace() async throws {
        let (model, c) = try conversation(40)
        let window = await host(model, c, UIScreen.main.bounds.size, arrive: false,
                                realKeyboard: true)
        try softwareKeyboard(true)
        defer { try? softwareKeyboard(false) }
        let top = try conversationView(window).contentOffset.y
        let composer = try XCTUnwrap(views(UITextView.self, in: window).last)
        for step in ["open", "closed", "reopened", "closed again"] {
            if step.hasPrefix("closed") {
                KeyboardLayout.dismiss()
                await waitForHeader(.full, window)
            } else {
                XCTAssertTrue(composer.becomeFirstResponder())
                await waitForHeader(.compact, window)
            }
            let list = try conversationView(window)
            XCTAssertEqual(list.contentOffset.y, top, accuracy: 1, "\(step) \(metrics(list))")
            XCTAssertFalse(atNewest(list), "\(step) \(metrics(list))")
            XCTAssertLessThanOrEqual(gap(list), 2, "\(step) gap \(gap(list))")
        }
        XCTAssertTrue(c.sent.isEmpty)
    }

    /// Dragging the conversation down into the keyboard dismisses it; scrolling elsewhere
    /// leaves it up, so history stays readable beside the compact header.
    func testTheConversationDismissesTheKeyboardWhenDraggedDown() async throws {
        let (model, c) = try conversation(12)
        let window = await host(model, c, screens[2])
        let list = try conversationView(window)
        // SwiftUI's interactively maps to the accessory variant; never none or on drag.
        XCTAssertTrue([.interactive, .interactiveWithAccessory].contains(list.keyboardDismissMode),
                      "keyboardDismissMode raw \(list.keyboardDismissMode.rawValue)")
    }

    /// A tap on the conversation puts the keyboard away: the draft and the reader's place stay.
    func testDismissingTheKeyboardKeepsTheDraftAndThePlace() async throws {
        let screen = screens[0]
        let (model, c) = try conversation(40)
        model.draft = "Half typed"
        let window = await host(model, c, screen, arrive: false)
        let composer = try XCTUnwrap(views(UITextView.self, in: window).last)
        XCTAssertTrue(composer.becomeFirstResponder())
        await openKeyboard(260, screen: screen, window)
        await settle { model.inputFocused }
        XCTAssertTrue(model.inputFocused)
        XCTAssertEqual(keyboard.mode, .compact)
        let list = try conversationView(window)
        let top = list.contentOffset.y
        XCTAssertFalse(atNewest(list), metrics(list))

        KeyboardLayout.dismiss()
        await closeKeyboard(screen: screen, window)
        await settle { !model.inputFocused }
        XCTAssertFalse(composer.isFirstResponder)
        XCTAssertFalse(model.inputFocused)
        XCTAssertEqual(keyboard.mode, .full)
        XCTAssertEqual(model.draft, "Half typed")
        let closed = try conversationView(window)
        XCTAssertEqual(closed.contentOffset.y, top, accuracy: 1, metrics(closed))
        XCTAssertTrue(c.sent.isEmpty)
        XCTAssertTrue(c.jacks.isEmpty)
    }

    /// The voice draft field gives up the keyboard the same way and keeps its text for review.
    func testDismissingTheKeyboardKeepsTheVoiceDraft() async throws {
        let screen = screens[2]
        let (model, c) = try conversation(12)
        await settle { model.listen.isPresent }
        model.listen.press()
        input.hear(.partial("Remind me after lunch"))
        model.listen.release()
        input.hear(.final("Remind me after lunch"))
        let window = await host(model, c, screen)
        let voice = try XCTUnwrap(views(UITextView.self, in: window).first)
        XCTAssertTrue(voice.becomeFirstResponder())
        await openKeyboard(336, screen: screen, window)
        voice.insertText(" today")
        await settle()

        KeyboardLayout.dismiss()
        await closeKeyboard(screen: screen, window)
        XCTAssertFalse(voice.isFirstResponder)
        XCTAssertEqual(keyboard.mode, .full)
        XCTAssertEqual(model.listen.phase, .reviewing)
        XCTAssertEqual(model.listen.draft, "Remind me after lunch today")
        XCTAssertTrue(c.sent.isEmpty)
    }

    /// The reviewed voice draft keeps its five line field beside the compact header.
    func testTheVoiceDraftKeepsItsLinesUnderTheCompactHeader() async throws {
        let transcript = "Stark, remind me to review the pull request after lunch, then check "
            + "whether the continuous integration run passed, and finally draft a note to the "
            + "team about the release schedule for next week."
        for screen in [screens[0], screens[2]] {
            let (model, c) = try conversation(12)
            await settle { model.listen.isPresent }
            model.listen.press()
            input.hear(.partial(transcript))
            model.listen.release()
            input.hear(.final(transcript))
            let window = await host(model, c, screen)
            let tag = "\(Int(screen.width))x\(Int(screen.height))"
            await openKeyboard(screen == screens[0] ? 260 : 336, screen: screen, window)
            XCTAssertEqual(keyboard.mode, .compact, tag)
            let fields = views(UITextView.self, in: window)
            XCTAssertEqual(fields.count, 2, tag)
            let voice = try XCTUnwrap(fields.first), composer = try XCTUnwrap(fields.last)
            XCTAssertGreaterThan(voice.bounds.height, 60, tag)
            XCTAssertGreaterThan(composer.bounds.height, 20, tag)
            XCTAssertLessThanOrEqual(frame(composer).maxY, window.bounds.maxY, tag)
            snapshot(window, "\(tag) voice draft open")
            voice.insertText(" Thanks")
            await settle()
            XCTAssertEqual(model.listen.draft, transcript + " Thanks", tag)
            XCTAssertEqual(model.listen.phase, .reviewing, tag)
            await closeKeyboard(screen: screen, window)
            XCTAssertEqual(keyboard.mode, .full, tag)
            XCTAssertGreaterThan(views(UITextView.self, in: window).first?.bounds.height ?? 0, 60, tag)
            XCTAssertEqual(model.listen.draft, transcript + " Thanks", tag)
            XCTAssertTrue(c.sent.isEmpty, tag)
            window.isHidden = true
        }
    }
}
