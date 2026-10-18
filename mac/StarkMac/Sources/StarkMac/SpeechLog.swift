import os
import StarkKit

/// Speech diagnostics in the unified log, subsystem dev.gameshark.starkmac, category speech.
/// SpeechDiagnostic lines only: seqs, tokens, voice identifiers, reasons, and timings, never
/// a reply's text. Read with `/usr/bin/log show`; architecture/stark-voice-output.md has the
/// capture steps.
enum SpeechLog {
    static let logger = Logger(subsystem: "dev.gameshark.starkmac", category: "speech")

    static let trace: Trace = { line in logger.notice("\(line, privacy: .public)") }

    static func note(_ d: SpeechDiagnostic) { trace(d.line) }
}
