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
