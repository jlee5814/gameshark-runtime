# Stark Pocket

Stark's iPhone surface. It shows the same Stark the Mac runs: identity, duties, and the
conversation live in the GameShark runtime on the Mac; the phone is a view that can hold
Stark's presence. Jack In pulls Stark onto the phone (StarkMac dims and turns read only);
Jack Out sends him home. Transport, pairing, and the threat model:
[architecture/stark-pocket-transport.md](../../architecture/stark-pocket-transport.md).

## What it stores

Only the pairing record and the device key, in the Keychain (this device only, after first
unlock; the key lives in the Secure Enclave when the hardware has one), plus presentation
facts in UserDefaults (the last reply viewed). Conversation rows are rebuilt from the
runtime on every connection.

## Build and test

Requires Xcode with an iOS 17 or later simulator runtime. The project is generated from
`project.yml` (`xcodegen`); the generated `StarkPocket.xcodeproj` is committed, so XcodeGen
is only needed after editing `project.yml`.

```sh
xcodebuild -project ios/StarkPocket/StarkPocket.xcodeproj -scheme StarkPocket \
  -destination 'platform=iOS Simulator,name=iPhone 18 Pro' test -only-testing:StarkPocketTests
ios/scripts/pocket-demo.sh            # live: runtime, StarkMac, simulator, faults
ios/scripts/pocket-demo.sh --skip-faults
```

`pocket-demo.sh` uses `~/.gameshark/demo` (fresh each run) and writes
`pocket-acceptance.md`, StarkMac snapshots, and simulator screenshots there.

## Lifecycle

Foreground: connect, authenticate, resync rows after the last contiguous seq, resend any
unsent message under its original id. Background: give a pending send up to 3 s, then
close cleanly; no background modes, no daemon, no overlay. Stark stays on the phone while
it is backgrounded; Jack In Here on the Mac takes him back (the silent phone is committed
past after the 3 s prepare deadline). A jack still open when the app leaves reverts.
Network changes trigger an immediate reconnect. A revoked device drops to the pairing
screen.

## On a physical iPhone

Not yet run. To try it:

1. In Xcode, open `StarkPocket.xcodeproj`, select the StarkPocket target, Signing &
   Capabilities, and choose your personal team (free provisioning works).
2. Put the iPhone and the Mac on the same Wi-Fi. On the Mac:
   `cd prototype && uv run python stark.py run --lan` and allow incoming connections if
   macOS asks. Start StarkMac.
3. Run the app on the iPhone from Xcode. Then on the Mac:
   `uv run --with qrcode==8.0 python stark.py pair --name "My iPhone"`.
4. Scan the QR with the iPhone Camera and open it in Stark Pocket; allow local network
   access when iOS asks.
5. Tap Jack In; StarkMac dims. Send a message; the reply arrives on the phone. Tap Jack
   Out; StarkMac becomes active with the same conversation.
6. Optional: `stark.py devices revoke <id>` and watch the phone return to pairing.
