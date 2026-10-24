import StarkKit
import XCTest
@testable import StarkPocket

/// The fake answers a pin the way the runtime would, through state, so tests read the
/// result the same way the stage does.
extension FakeClient: PocketLoadoutClient {
    func setLoadout(_ pin: Loadout?) -> String {
        emit {
            $0.loadoutOp = nil
            $0.loadout.pinned = pin
            $0.loadout.effective = pin ?? $0.loadout.inferred
        }
        return "lo"
    }
}

final class LoadoutSpriteTests: XCTestCase {
    func testTheGymAtlasIsBundledBesideTheStandardOne() throws {
        let atlas = try XCTUnwrap(PocketAtlas.load(bundle: Bundle(for: PocketModel.self)))
        let gym = try XCTUnwrap(atlas.gymFrames)
        XCTAssertEqual(gym.count, atlas.frames.count)
        XCTAssertEqual(gym.count, 74)
    }
}

/// Phase 4.4B: the runtime decides what Stark wears; the phone draws it and asks for pins.
@MainActor
final class PocketLoadoutTests: PocketHarness {
    func testARuntimeLoadoutSwitchesTheSameFrameToItsGymTwin() async throws {
        let model = try pairedModel()
        let c = clients[0]
        model.networkChanged(.wifi)
        holding(c)
        let stage = try XCTUnwrap(model.stage)
        await settle { stage.loadout == .standard }
        let standard = stage.image
        let frame = stage.frame
        c.emit { $0.loadout = LoadoutState(effective: .gym, inferred: .gym, pinned: nil) }
        await settle { stage.loadout == .gym }
        XCTAssertEqual(stage.loadout, .gym)
        if stage.frame == frame {
            XCTAssertFalse(stage.image === standard)
            XCTAssertTrue(stage.image === stage.atlas.gymFrames?[frame])
        }
        c.emit { $0.loadout = .standard }
        await settle { stage.loadout == .standard }
        XCTAssertTrue(stage.image === stage.atlas.frames[stage.frame])
    }

    func testThinkingStillAnimatesInGymForm() async throws {
        let model = try pairedModel()
        let c = clients[0]
        model.networkChanged(.wifi)
        holding(c)
        c.emit {
            $0.loadout = LoadoutState(effective: .gym, inferred: .gym, pinned: nil)
            $0.working = true
        }
        let stage = try XCTUnwrap(model.stage)
        await settle { stage.animation == .thinking && stage.loadout == .gym }
        XCTAssertEqual(stage.animation, .thinking)
        XCTAssertEqual(stage.loadout, .gym)
    }

    func testSlashCommandsPinWithoutChatOrSpeech() async throws {
        let model = try pairedModel()
        let c = clients[0]
        model.networkChanged(.wifi)
        holding(c)
        c.emit { $0.loadoutAvailable = true }
        await settle { model.state?.loadoutAvailable == true }
        model.draft = "/gym"
        model.send()
        XCTAssertEqual(model.draft, "")
        XCTAssertEqual(c.state.loadout.pinned, .gym)
        XCTAssertEqual(c.state.shownLoadout, .gym)
        model.draft = "/auto"
        model.send()
        XCTAssertNil(c.state.loadout.pinned)
        XCTAssertEqual(c.state.shownLoadout, .standard)
        XCTAssertEqual(c.sent, [])
        XCTAssertEqual(c.remembered, [])
        XCTAssertEqual(c.jacks, [])
        XCTAssertEqual(c.retries, [])
        XCTAssertNil(model.voice.speakingSeq)
    }

    func testAnOlderRuntimeLeavesTheCommandInTheComposer() async throws {
        let model = try pairedModel()
        let c = clients[0]
        model.networkChanged(.wifi)
        holding(c)
        await settle { model.state != nil }
        model.draft = "/gym"
        model.send()
        XCTAssertEqual(model.draft, "/gym")
        XCTAssertEqual(c.sent, [])
        XCTAssertNil(c.state.loadout.pinned)
    }

    func testSayingYoureAtTheGymIsOrdinaryChat() async throws {
        let model = try pairedModel()
        let c = clients[0]
        model.networkChanged(.wifi)
        holding(c)
        c.emit { $0.loadoutAvailable = true }
        await settle { model.state?.loadoutAvailable == true }
        model.draft = "I'm at the gym"
        model.send()
        // Sent once as a message; the runtime infers the loadout, the phone never does.
        XCTAssertEqual(c.sent, ["I'm at the gym"])
        XCTAssertNil(c.state.loadout.pinned)
        XCTAssertEqual(c.state.shownLoadout, .standard)
    }

    func testAReconnectSnapshotRestoresTheLoadout() async throws {
        let model = try pairedModel()
        let c = clients[0]
        model.networkChanged(.wifi)
        holding(c)
        c.emit { $0.loadout = LoadoutState(effective: .gym, inferred: .gym, pinned: nil) }
        let stage = try XCTUnwrap(model.stage)
        await settle { stage.loadout == .gym }
        holding(c, live: false)                    // the last value stays while away
        await settle { model.state?.connection.isLive == false }
        XCTAssertEqual(stage.loadout, .gym)
        c.emit {                                   // a pin from the Mac landed meanwhile
            $0.connection = .live
            $0.loadout = LoadoutState(effective: .standard, inferred: .gym, pinned: .standard)
        }
        await settle { stage.loadout == .standard }
        XCTAssertEqual(stage.loadout, .standard)
    }
}
