import XCTest

/// Drives the real app in the simulator against a real runtime on this Mac. The runner
/// passes the pairing link as TEST_RUNNER_STARK_PAIR_LINK (see ios/scripts/pocket-demo.sh);
/// without it the test skips.
@MainActor
final class PocketHandoffUITests: XCTestCase {
    var app: XCUIApplication!

    /// Launch inside the test, not setUp: XCTestCase.setUp is nonisolated on older SDKs.
    func launch() throws {
        continueAfterFailure = false
        guard let link = ProcessInfo.processInfo.environment["STARK_PAIR_LINK"] else {
            throw XCTSkip("needs a live runtime; run ios/scripts/pocket-demo.sh")
        }
        app = XCUIApplication()
        app.launchEnvironment = ["STARK_RESET": "1", "STARK_PAIR_LINK": link]
        app.launch()
    }

    func shot(_ name: String) {
        let a = XCTAttachment(screenshot: app.screenshot())
        a.name = name
        a.lifetime = .keepAlways
        add(a)
    }

    func waitFor(_ id: String, label: String, timeout: TimeInterval = 30) {
        let e = app.descendants(matching: .any)[id].firstMatch
        let p = NSPredicate(format: "label CONTAINS %@", label)
        let ok = XCTNSPredicateExpectation(predicate: p, object: e)
        XCTAssertEqual(XCTWaiter().wait(for: [ok], timeout: timeout), .completed,
                       "\(id) never showed \(label); it shows \(e.label)")
    }

    func waitForText(_ text: String, timeout: TimeInterval = 30) {
        let e = app.staticTexts.containing(NSPredicate(format: "label CONTAINS %@", text)).firstMatch
        XCTAssertTrue(e.waitForExistence(timeout: timeout), "never saw \(text)")
    }

    func testJackInTalkAndJackOut() throws {
        try launch()
        // 1, 2: Stark is on the Mac; the phone paired and sees him there, read only.
        waitFor("presenceLabel", label: "Stark is on Mac")
        shot("1-paired-stark-on-mac")
        // 3 to 5: Jack In.
        app.buttons["jackButton"].tap()
        waitFor("presenceLabel", label: "Stark is here")
        waitFor("connectionLabel", label: "live")
        shot("2-jacked-in")
        // 7, 8: talk to the same Stark; the reply commits through the runtime's journal.
        let field = app.textFields["composer"].exists ? app.textFields["composer"]
            : app.textViews["composer"]
        let signal = ProcessInfo.processInfo.environment["STARK_SIGNAL_DIR"]
        if signal != nil {
            // Mac -> iPhone: StarkMac's unsent text arrived in this composer, not sent.
            let restored = XCTNSPredicateExpectation(
                predicate: NSPredicate(format: "value == %@", "Hello from my pocket"), object: field)
            XCTAssertEqual(XCTWaiter().wait(for: [restored], timeout: 15), .completed,
                           "the Mac's draft never reached the composer; it shows \(field.value ?? "")")
            shot("2b-mac-draft-restored")
        } else {
            field.tap()
            field.typeText("Hello from my pocket")
        }
        let rows = app.staticTexts.matching(NSPredicate(format: "identifier BEGINSWITH 'row-'"))
        let before = rows.count
        app.buttons["sendButton"].tap()
        if ProcessInfo.processInfo.environment["STARK_LIVE_MODEL"] == "1" {
            // A real model's words are unknown: expect the question and one answer bubble.
            let answered = XCTNSPredicateExpectation(
                predicate: NSPredicate(format: "count >= %d", before + 2), object: rows)
            XCTAssertEqual(XCTWaiter().wait(for: [answered], timeout: 90), .completed,
                           "no answer bubble; \(rows.count) rows, \(before) before sending")
        } else {
            waitForText("You said: Hello from my pocket", timeout: 45)
        }
        shot("3-reply-on-phone")
        // 9: Jack Out to the Mac.
        app.buttons["jackButton"].tap()
        waitFor("presenceLabel", label: "Stark is on Mac")
        shot("4-jacked-out")
        // Jack In again and leave text unsent; the Mac takes Stark back with Jack In Here,
        // and the phone's draft travels in the handoff journal.
        guard let signal else { return }
        app.buttons["jackButton"].tap()
        waitFor("presenceLabel", label: "Stark is here")
        field.tap()
        field.typeText("unsent from my pocket")
        let marker = URL(fileURLWithPath: signal).appendingPathComponent("draft-typed")
        try Data().write(to: marker)
        waitFor("presenceLabel", label: "Stark is on Mac")
        // The text left with Stark, so this composer lets it go.
        let cleared = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "NOT (value CONTAINS %@)", "unsent from my pocket"),
            object: field)
        XCTAssertEqual(XCTWaiter().wait(for: [cleared], timeout: 15), .completed,
                       "the phone kept the draft that left: \(field.value ?? "")")
        shot("5-mac-jacked-in-with-draft")
    }
}

/// Faults on the simulator path, coordinated with pocket_acceptance.py through what the
/// runtime records: the script restarts the runtime once this phone holds presence and
/// has sent "restart now", then revokes the device once "after restart" is answered.
@MainActor
final class PocketFaultUITests: XCTestCase {
    func testRuntimeRestartThenRevocation() throws {
        guard let link = ProcessInfo.processInfo.environment["STARK_PAIR_LINK"] else {
            throw XCTSkip("needs a live runtime; run prototype/tools/pocket_acceptance.py")
        }
        let app = XCUIApplication()
        app.launchEnvironment = ["STARK_RESET": "1", "STARK_PAIR_LINK": link]
        app.launch()
        let presence = app.descendants(matching: .any)["presenceLabel"].firstMatch
        let connection = app.descendants(matching: .any)["connectionLabel"].firstMatch
        func until(_ e: XCUIElement, _ text: String, _ t: TimeInterval = 40) {
            let ok = XCTNSPredicateExpectation(predicate: NSPredicate(format: "label CONTAINS %@", text),
                                               object: e)
            XCTAssertEqual(XCTWaiter().wait(for: [ok], timeout: t), .completed,
                           "never saw \(text); shows \(e.label)")
        }
        func say(_ text: String) {
            let field = app.textFields["composer"].exists ? app.textFields["composer"]
                : app.textViews["composer"]
            field.tap()
            field.typeText(text)
            app.buttons["sendButton"].tap()
        }
        func shot(_ name: String) {
            let a = XCTAttachment(screenshot: app.screenshot())
            a.name = name
            a.lifetime = .keepAlways
            add(a)
        }
        until(presence, "Stark is on")
        app.buttons["jackButton"].tap()
        until(presence, "Stark is here")
        say("restart now")
        // The script restarts the runtime: the phone loses it, then comes back by itself.
        until(presence, "Mac unreachable", 30)
        shot("5-runtime-down")
        until(connection, "live", 60)
        until(presence, "Stark is here")    // presence survived the restart
        shot("6-back-after-restart")
        say("after restart")
        // The script revokes this device: the app drops to the pairing screen.
        let pairTitle = app.navigationBars["Pair Stark Pocket"]
        XCTAssertTrue(pairTitle.waitForExistence(timeout: 60), "revocation never reached the app")
        shot("7-revoked")
    }
}
