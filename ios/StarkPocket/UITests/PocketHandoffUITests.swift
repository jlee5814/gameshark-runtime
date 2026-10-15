import XCTest

/// Drives the real app in the simulator against a real runtime on this Mac. The runner
/// passes the pairing link as TEST_RUNNER_STARK_PAIR_LINK (see ios/scripts/pocket-demo.sh);
/// without it the test skips.
final class PocketHandoffUITests: XCTestCase {
    var app: XCUIApplication!

    override func setUpWithError() throws {
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

    func testJackInTalkAndJackOut() {
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
        field.tap()
        field.typeText("Hello from my pocket")
        app.buttons["sendButton"].tap()
        waitForText("You said: Hello from my pocket", timeout: 45)
        shot("3-reply-on-phone")
        // 9: Jack Out to the Mac.
        app.buttons["jackButton"].tap()
        waitFor("presenceLabel", label: "Stark is on Mac")
        shot("4-jacked-out")
    }
}

/// Faults on the simulator path, coordinated with pocket_acceptance.py through what the
/// runtime records: the script restarts the runtime once this phone holds presence and
/// has sent "restart now", then revokes the device once "after restart" is answered.
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
