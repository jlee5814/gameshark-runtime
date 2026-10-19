import Foundation
import Testing
@testable import StarkKit

/// The order things happened in across the engine and the microphone.
@MainActor
final class CallLog {
    var calls: [String] = []
}

/// A speech engine that records its calls into a shared log; plays nothing.
@MainActor
final class LoggedEngine: SpeechEngine {
    let log: CallLog
    var spoken: [Int] = []
    var playing: Utterance.ID?
    var onFinish: ((Utterance.ID) -> Void)?
    var onInterrupt: (() -> Void)?

    init(_ log: CallLog) { self.log = log }

    func speak(_ u: Utterance) {
        spoken.append(u.id.seq)
        playing = u.id
        log.calls.append("engine.speak \(u.id.seq)")
    }

    func stop() {
        playing = nil
        log.calls.append("engine.stop")
    }
}

/// A scripted recognizer: records what it was asked and replays what the test says.
@MainActor
final class FakeSpeechInput: SpeechInput {
    let log: CallLog
    var availability: InputAvailability = .ready
    var availabilityChanged: (@MainActor () -> Void)?
    var permissionRequests = 0
    var starts: [Int] = []
    var finishes = 0
    var cancels = 0
    private var sink: (@MainActor (Int, InputEvent) -> Void)?
    private var pending: (@MainActor (InputAvailability) -> Void)?

    init(_ log: CallLog) { self.log = log }

    func requestPermission(_ done: @escaping @MainActor (InputAvailability) -> Void) {
        permissionRequests += 1
        pending = done
    }

    /// The user answered the system prompt.
    func answer(_ result: InputAvailability) {
        availability = result
        pending?(result)
        pending = nil
    }

    func start(session: Int, _ events: @escaping @MainActor (Int, InputEvent) -> Void) {
        starts.append(session)
        sink = events
        log.calls.append("input.start \(session)")
    }

    func finish() { finishes += 1 }

    func cancel() {
        cancels += 1
        log.calls.append("input.cancel")
    }

    /// The recognizer reports, for the newest session unless told otherwise. A real input
    /// drops events after cancel; the fake keeps the sink so late callbacks can be tested.
    func hear(_ event: InputEvent, session: Int? = nil) {
        sink?(session ?? starts.last ?? 0, event)
    }
}

/// What the surface did with the voice draft.
@MainActor
final class Surface {
    var submitted: [String] = []
    var inserted: [String] = []
    var accepts = true
}

@MainActor
final class Rig {
    let log = CallLog()
    let engine: LoggedEngine
    let input: FakeSpeechInput
    let voice: SpeechCoordinator
    let capture: CaptureCoordinator
    let surface = Surface()
    let lines = LineLog()
    let feed: Feed

    init(holder: String = "mac", availability: InputAvailability = .ready) {
        engine = LoggedEngine(log)
        input = FakeSpeechInput(log)
        input.availability = availability
        voice = SpeechCoordinator(engine: engine, store: MemorySpeechStore())
        let surface = self.surface
        capture = CaptureCoordinator(input: input, speech: voice, trace: lines.trace,
                                     submit: { text in
                                         surface.submitted.append(text)
                                         return surface.accepts
                                     },
                                     insert: { surface.inserted.append($0) })
        feed = Feed(holder: holder, rows: history(2))
        publish(feed.next())
    }

    func publish(_ s: ClientState) {
        voice.observe(s)
        capture.observe(s)
    }

    /// Hold, hear `text`, release, and get the final transcript: a draft ready to review.
    func dictate(_ text: String) {
        capture.press()
        input.hear(.partial(String(text.prefix(4))))
        capture.release()
        input.hear(.final(text))
    }
}

@MainActor @Suite struct CaptureTests {
    // MARK: permissions

    @Test func theFirstPressAsksPermissionAndNeverStartsListening() {
        let rig = Rig(availability: .needsPermission)
        rig.capture.press()
        #expect(rig.input.permissionRequests == 1)
        #expect(rig.capture.phase == .requesting)
        rig.capture.release()                           // the finger lifts during the prompt
        rig.input.answer(.ready)
        #expect(rig.input.starts.isEmpty)
        #expect(rig.capture.phase == .idle)
        #expect(rig.capture.notice == "Ready. Hold the microphone to talk.")
        rig.capture.press()                             // a fresh, deliberate press
        #expect(rig.input.starts == [1])
        #expect(rig.input.permissionRequests == 1)
    }

    @Test func deniedOrRestrictedAccessNeverPromptsOrListens() {
        for a in [InputAvailability.denied, .restricted] {
            let rig = Rig(availability: a)
            rig.capture.press()
            #expect(rig.input.permissionRequests == 0)
            #expect(rig.input.starts.isEmpty)
            #expect(rig.capture.notice == a.explanation)
            #expect(!rig.capture.canPress)
        }
        let rig = Rig(availability: .needsPermission)
        rig.capture.press()
        rig.input.answer(.denied)
        #expect(rig.capture.phase == .idle)
        #expect(rig.input.starts.isEmpty)
        #expect(!rig.capture.canPress)
    }

    @Test func aPressReadsAccessAgainAfterSettingsChange() {
        let rig = Rig(availability: .denied)
        rig.capture.press()
        #expect(rig.input.starts.isEmpty)
        rig.input.availability = .ready                 // turned on in Settings
        rig.capture.press()
        #expect(rig.input.starts == [1])
    }

    @Test func aRecognizerComingBackRefreshesAvailability() {
        let rig = Rig(availability: .unavailable(.offline))
        #expect(!rig.capture.canPress)
        rig.input.availability = .ready
        rig.input.availabilityChanged?()
        #expect(rig.capture.canPress)
    }

    @Test func withoutOnDeviceRecognitionVoiceInputStaysOff() {
        for u in [InputAvailability.Unavailable.noOnDevice, .noRecognizer, .notConfigured,
                  .offline] {
            let rig = Rig(availability: .unavailable(u))
            #expect(!rig.capture.canPress)
            rig.capture.press()
            #expect(rig.input.starts.isEmpty && rig.input.permissionRequests == 0)
            #expect(rig.capture.notice == InputAvailability.unavailable(u).explanation)
        }
        #expect(InputAvailability.unavailable(.noOnDevice).explanation?.contains("server") == true)
    }

    // MARK: transcription and review

    @Test func partialAndFinalResultsFillTheDraftAndReleaseKeepsItForReview() {
        let rig = Rig()
        rig.capture.press()
        #expect(rig.capture.isListening)
        rig.input.hear(.partial("turn on"))
        #expect(rig.capture.draft == "turn on")
        rig.input.hear(.partial("turn on the lights"))
        rig.capture.release()
        #expect(rig.input.finishes == 1)
        #expect(rig.capture.phase == .finishing(1))
        rig.input.hear(.final("Turn on the lights."))
        #expect(rig.capture.phase == .reviewing)
        #expect(rig.capture.draft == "Turn on the lights.")
        #expect(rig.input.cancels == 1)                 // the microphone is released
        #expect(rig.surface.submitted.isEmpty)          // never sent on its own
    }

    @Test func theDraftIsEditableOnlyInReview() {
        let rig = Rig()
        rig.capture.press()
        rig.input.hear(.partial("hello"))
        rig.capture.edit("typed over")
        #expect(rig.capture.draft == "hello")
        rig.capture.release()
        rig.input.hear(.final("hello"))
        rig.capture.edit("hello there")
        #expect(rig.capture.draft == "hello there")
        #expect(rig.capture.phase == .reviewing)
    }

    @Test func anotherPressAppendsToTheReviewedDraft() {
        let rig = Rig()
        rig.dictate("Hello.")
        rig.capture.press()
        rig.input.hear(.partial("again"))
        #expect(rig.capture.draft == "Hello. again")
        rig.capture.release()
        rig.input.hear(.final("again."))
        #expect(rig.capture.draft == "Hello. again.")
    }

    @Test func aFinalArrivingWhileHeldEndsCaptureForReview() {
        let rig = Rig()
        rig.capture.press()
        rig.input.hear(.final("done already"))
        #expect(rig.capture.phase == .reviewing)
        rig.capture.release()
        #expect(rig.input.finishes == 0)
    }

    @Test func releasingWithNothingHeardLeavesNoDraft() {
        let rig = Rig()
        rig.capture.press()
        rig.capture.release()
        rig.input.hear(.final("  "))
        #expect(rig.capture.phase == .idle)
        #expect(rig.capture.notice == "Stark didn't catch that.")
    }

    @Test func aFinishTimeoutKeepsWhatWasHeard() {
        var m = CaptureMachine()
        _ = m.step(.state(Feed().next()))
        _ = m.step(.press(.ready))
        _ = m.step(.input(1, .partial("almost there")))
        #expect(m.step(.release) == [.finishRecognizer(1)])
        #expect(m.step(.finishTimeout(1)) == [.cancelRecognizer, .releaseSpeech])
        #expect(m.phase == .reviewing)
        #expect(m.draft == "almost there")
        #expect(m.step(.finishTimeout(1)) == [])
    }

    // MARK: discarding

    @Test func cancelAndEscapeDiscardAndLateCallbacksChangeNothing() {
        for why in [CaptureMachine.Reason.user, .escape] {
            let rig = Rig()
            rig.capture.press()
            rig.input.hear(.partial("secret plan"))
            rig.capture.cancel(why)
            #expect(rig.capture.phase == .idle)
            #expect(rig.capture.draft.isEmpty)
            #expect(rig.input.cancels == 1)
            rig.input.hear(.partial("secret plan B"))
            rig.input.hear(.final("secret plan C"))
            #expect(rig.capture.draft.isEmpty)
            #expect(rig.capture.phase == .idle)
        }
    }

    @Test func aCallbackFromAnOlderSessionNeverReachesTheNewOne() {
        let rig = Rig()
        rig.capture.press()
        rig.capture.cancel()
        rig.capture.press()
        #expect(rig.input.starts == [1, 2])
        rig.input.hear(.partial("stale words"), session: 1)
        rig.input.hear(.final("stale words"), session: 1)
        #expect(rig.capture.draft.isEmpty)
        #expect(rig.capture.phase == .capturing(2))
    }

    @Test func backgroundAndSleepDiscardWhileListeningAndInReview() {
        for why in [CaptureMachine.Reason.background, .sleep, .chatClosed] {
            let rig = Rig()
            rig.capture.press()
            rig.input.hear(.partial("half"))
            rig.capture.cancel(why)
            #expect(rig.capture.phase == .idle && rig.capture.draft.isEmpty)
            #expect(rig.input.cancels == 1)
            rig.dictate("reviewed text")
            rig.capture.cancel(why)
            #expect(rig.capture.phase == .idle && rig.capture.draft.isEmpty)
        }
    }

    @Test func aJackOpeningDuringCaptureDiscardsAndBlocksPressing() {
        let rig = Rig()
        rig.capture.press()
        rig.input.hear(.partial("going to"))
        rig.publish(rig.feed.next {
            $0.handoff = HandoffInfo(id: "h1", from: "mac", to: "pocket-a1b2c3",
                                     state: "preparing", fromGen: 1, toGen: nil)
        })
        #expect(rig.capture.phase == .idle && rig.capture.draft.isEmpty)
        #expect(rig.input.cancels == 1)
        #expect(rig.capture.notice == "Voice draft discarded: Stark moved or the link dropped.")
        #expect(!rig.capture.canPress)
        rig.capture.press()
        #expect(rig.input.starts == [1])
    }

    @Test func aPresenceGenerationChangeDiscardsAReviewedDraft() {
        let rig = Rig()
        rig.dictate("keep this?")
        rig.publish(rig.feed.next {
            $0.presence = PresenceInfo(surface: "mac", home: "mac", generation: 2)
        })
        #expect(rig.capture.phase == .idle && rig.capture.draft.isEmpty)
    }

    @Test func aGhostSurfaceCannotListen() {
        let rig = Rig(holder: "pocket-a1b2c3")
        #expect(!rig.capture.canPress)
        rig.capture.press()
        #expect(rig.input.starts.isEmpty && rig.input.permissionRequests == 0)
        #expect(rig.capture.notice == "Jack In to talk to Stark here.")
    }

    @Test func aDisconnectDiscardsDuringRecognitionAndReview() {
        let rig = Rig()
        rig.capture.press()
        rig.input.hear(.partial("mid"))
        rig.publish(rig.feed.next { $0.connection = .disconnected("runtime closed") })
        #expect(rig.capture.phase == .idle && rig.input.cancels == 1)
        rig.publish(rig.feed.next { $0.connection = .live })
        rig.dictate("reviewed")
        rig.publish(rig.feed.next { $0.connection = .syncing })
        #expect(rig.capture.phase == .idle && rig.capture.draft.isEmpty)
    }

    @Test func anInterruptionOrRecognizerFailureDiscards() {
        for event in [InputEvent.interrupted, .failed(code: 1110)] {
            let rig = Rig()
            rig.capture.press()
            rig.input.hear(.partial("words"))
            rig.input.hear(event)
            #expect(rig.capture.phase == .idle && rig.capture.draft.isEmpty)
            #expect(rig.input.cancels == 1)
        }
    }

    @Test func aStaleStateNeverDiscards() {
        let rig = Rig()
        let old = rig.feed.next { $0.connection = .disconnected("old") }
        let fresh = rig.feed.next { $0.connection = .live }
        rig.publish(fresh)
        rig.dictate("still here")
        rig.capture.observe(old)                        // delivered after a newer state
        #expect(rig.capture.phase == .reviewing)
    }

    // MARK: send and insert

    @Test func sendSubmitsTheReviewedTextExactlyOnce() {
        let rig = Rig()
        rig.dictate("  Hi Stark, what's next?  ")
        rig.capture.edit("Hi Stark, what's next on the list?")
        rig.capture.send()
        rig.capture.send()
        #expect(rig.surface.submitted == ["Hi Stark, what's next on the list?"])
        #expect(rig.capture.phase == .idle && rig.capture.draft.isEmpty)
        #expect(rig.surface.inserted.isEmpty)
    }

    @Test func aRefusedSubmitKeepsTheDraftForReview() {
        let rig = Rig()
        rig.surface.accepts = false
        rig.dictate("try later")
        rig.capture.send()
        #expect(rig.capture.phase == .reviewing)
        #expect(rig.capture.draft == "try later")
    }

    @Test func nothingIsSubmittedWhileListeningOrWithoutReview() {
        let rig = Rig()
        rig.capture.send()
        rig.capture.press()
        rig.input.hear(.partial("not yet"))
        rig.capture.send()
        rig.capture.insertIntoComposer()
        rig.capture.release()
        rig.input.hear(.final("not yet"))
        #expect(rig.surface.submitted.isEmpty && rig.surface.inserted.isEmpty)
    }

    @Test func insertHandsTheTextToTheComposerAndClearsTheVoiceDraft() {
        let rig = Rig()
        rig.dictate("remind me to stretch")
        rig.capture.insertIntoComposer()
        rig.capture.insertIntoComposer()
        #expect(rig.surface.inserted == ["remind me to stretch"])
        #expect(rig.surface.submitted.isEmpty)
        #expect(rig.capture.phase == .idle && rig.capture.draft.isEmpty)
    }

    // MARK: Stark's voice

    @Test func starkStopsTalkingBeforeTheMicrophoneOpens() {
        let rig = Rig()
        rig.publish(rig.feed.reply("A long answer"))
        #expect(rig.engine.playing != nil)
        rig.capture.press()
        let stop = rig.log.calls.lastIndex(of: "engine.stop")
        let start = rig.log.calls.lastIndex(of: "input.start 1")
        #expect(stop != nil && start != nil && stop! < start!)
        #expect(rig.voice.speakingSeq == nil)
    }

    @Test func aReplyDuringCaptureIsNeverSpokenAndNothingResumes() {
        let rig = Rig()
        rig.capture.press()
        rig.publish(rig.feed.reply("arrived while listening"))
        #expect(rig.engine.spoken.isEmpty)
        #expect(!rig.voice.canReplay)
        rig.capture.release()
        rig.input.hear(.final("question"))
        #expect(rig.engine.spoken.isEmpty)
        #expect(rig.voice.canReplay)                    // replay returns at once
        rig.publish(rig.feed.reply("the next one"))
        #expect(rig.engine.spoken == [4])
    }

    @Test func aStaleStateDuringCaptureNeverBlocksTheNextReply() {
        let rig = Rig()
        let old = rig.feed.next()
        rig.publish(rig.feed.next())
        rig.capture.press()
        rig.voice.observe(old)                          // delivered late, while listening
        rig.capture.release()
        rig.input.hear(.final("question"))
        #expect(rig.voice.canReplay)
        rig.publish(rig.feed.reply("the answer"))
        #expect(rig.engine.spoken == [3])
    }

    @Test func aDiscardedCaptureAlsoReleasesStarksVoice() {
        let rig = Rig()
        rig.capture.press()
        rig.capture.cancel(.escape)
        rig.publish(rig.feed.reply("after"))
        #expect(rig.engine.spoken == [3])
    }

    // MARK: privacy and authority

    @Test func noDiagnosticLineCarriesTheTranscript() {
        let rig = Rig()
        let secret = "Zebra launch code 7741 tonight"
        rig.capture.press()
        rig.input.hear(.partial(secret))
        rig.capture.release()
        rig.input.hear(.final(secret))
        rig.capture.edit(secret + " please")
        rig.capture.send()
        rig.dictate(secret)
        rig.capture.insertIntoComposer()
        rig.dictate(secret)
        rig.capture.cancel(.escape)
        #expect(rig.lines.lines.contains("capture start session=1"))
        #expect(rig.lines.lines.contains { $0.hasPrefix("capture send chars=") })
        for word in secret.split(separator: " ") where word.count > 3 {
            #expect(!rig.lines.lines.contains { $0.contains(word) }, "leaked \(word)")
        }
    }

    @Test func theCaptureHoldsNoClient() {
        let rig = Rig()
        let held = Mirror(reflecting: rig.capture).children.map { "\(type(of: $0.value))" }
        #expect(!held.contains { $0.contains("GatewayClient") })
    }

    @Test func captureLinesRenderStably() {
        let cases: [(SpeechDiagnostic, String)] = [
            (.capture(.rejected(.notPresent)), "capture rejected reason=not_present"),
            (.capture(.rejected(.busy)), "capture rejected reason=busy"),
            (.capture(.rejected(.availability(.unavailable(.noOnDevice)))),
             "capture rejected reason=unavailable_no_on_device"),
            (.capture(.askedPermission), "capture permission asked"),
            (.capture(.permission(.denied)), "capture permission result=denied"),
            (.capture(.started(3)), "capture start session=3"),
            (.capture(.captured(3, chars: 12)), "capture end session=3 chars=12"),
            (.capture(.timedOut(3, chars: 5)), "capture end session=3 chars=5 timed_out=true"),
            (.capture(.discarded(.chatClosed, chars: 4)), "capture discard reason=chat_closed chars=4"),
            (.capture(.sent(chars: 9)), "capture send chars=9"),
            (.capture(.inserted(chars: 9)), "capture insert chars=9"),
            (.listening(true), "listening on"),
            (.disarmed(.listening), "disarmed reason=listening"),
        ]
        for (d, line) in cases { #expect(d.line == line) }
    }
}

/// Records what the claims ask of the session.
@MainActor
final class FakeAudioSession: AudioSessionPort {
    var calls: [String] = []
    var failActivate = false
    var failCategory = false

    func setCategory(_ config: AudioSessionConfig) throws {
        if failCategory { throw NSError(domain: "test", code: -50) }
        calls.append("category \(config)")
    }

    func setActive(_ active: Bool) throws {
        if active && failActivate { throw NSError(domain: "test", code: 561017449) }
        calls.append(active ? "on" : "off")
    }
}

@MainActor @Suite struct AudioSessionClaimsTests {
    @Test func speechAloneActivatesAndDeactivatesAsBefore() {
        let port = FakeAudioSession()
        let claims = AudioSessionClaims(port: port)
        #expect(claims.acquire(.speech) == (true, [.activated]))
        #expect(claims.release(.speech) == [.deactivated])
        #expect(port.calls == ["category playback", "on", "off"])
    }

    @Test func aLateSpeechReleaseNeverDeactivatesAnOpenMicrophone() {
        let port = FakeAudioSession()
        let claims = AudioSessionClaims(port: port)
        _ = claims.acquire(.speech)
        _ = claims.acquire(.capture)
        #expect(claims.release(.speech).isEmpty)        // the stopped utterance ends late
        #expect(claims.active)
        #expect(claims.release(.capture) == [.deactivated])
        #expect(port.calls == ["category playback", "on", "category record", "off"])
    }

    @Test func aFailedActivationHoldsNoClaim() {
        let port = FakeAudioSession()
        port.failActivate = true
        let claims = AudioSessionClaims(port: port)
        let r = claims.acquire(.capture)
        #expect(!r.ok)
        #expect(r.events == [.activateFailed(code: 561017449)])
        #expect(claims.claims.isEmpty)
        #expect(claims.release(.capture).isEmpty)
    }

    @Test func captureEndingBeforeALateSpeechReleaseRestoresPlayback() {
        let port = FakeAudioSession()
        let claims = AudioSessionClaims(port: port)
        _ = claims.acquire(.speech)
        _ = claims.acquire(.capture)
        #expect(claims.release(.capture).isEmpty)       // recognition ends first
        #expect(claims.active)
        #expect(port.calls == ["category playback", "on", "category record", "category playback"])
        #expect(claims.release(.speech) == [.deactivated])
    }

    @Test func captureNeverRunsWithoutTheRecordCategory() {
        let port = FakeAudioSession()
        port.failCategory = true
        let claims = AudioSessionClaims(port: port)
        let r = claims.acquire(.capture)
        #expect(!r.ok)
        #expect(r.events == [.categoryFailed(code: -50)])
        #expect(!claims.active && claims.claims.isEmpty)
        // Speech keeps its old rule: the category failure is logged and play goes on.
        #expect(claims.acquire(.speech) == (true, [.categoryFailed(code: -50), .activated]))
    }

    @Test func speechAfterCaptureReturnsToPlayback() {
        let port = FakeAudioSession()
        let claims = AudioSessionClaims(port: port)
        _ = claims.acquire(.capture)
        _ = claims.release(.capture)
        _ = claims.acquire(.speech)
        #expect(port.calls == ["category record", "on", "off", "category playback", "on"])
    }
}

@MainActor @Suite struct SpeechListeningTests {
    @Test func listeningDisarmsAndRepliesMeanwhileStayHistory() {
        var m = SpeechMachine()
        let feed = Feed()
        _ = m.step(.state(feed.next()), load: { _ in nil })
        _ = m.step(.state(feed.reply("one")), load: { _ in nil })
        #expect(m.step(.setListening(true), load: { _ in nil }) == [.stopEngine])
        #expect(m.gate == .disarmed(.listening))
        let fx = m.step(.state(feed.reply("two")), load: { _ in nil })
        #expect(!fx.contains { if case .speak = $0 { true } else { false } })
        _ = m.step(.setListening(false), load: { _ in nil })
        _ = m.step(.state(feed.next()), load: { _ in nil })
        #expect(m.isArmed)
        #expect(m.handled == 2)
        let next = m.step(.state(feed.reply("three")), load: { _ in nil })
        #expect(next.contains { if case .speak(let id, _) = $0 { id.seq == 3 } else { false } })
    }

    @Test func listeningAndSuspensionAreIndependent() {
        var m = SpeechMachine()
        let feed = Feed()
        _ = m.step(.state(feed.next()), load: { _ in nil })
        _ = m.step(.setSuspended(true), load: { _ in nil })
        _ = m.step(.setListening(true), load: { _ in nil })
        _ = m.step(.setListening(false), load: { _ in nil })
        _ = m.step(.state(feed.next()), load: { _ in nil })
        #expect(m.gate == .disarmed(.suspended))
    }
}
