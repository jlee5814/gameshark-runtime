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

Not yet run on a device; the simulator acceptance is the only evidence so far. The
checklist below uses its own database, never `~/.gameshark/runtime.db` or the simulator's
`~/.gameshark/demo`. Run the Mac commands from `prototype/`.

```sh
export DB=~/.gameshark/device-check/runtime.db && mkdir -p "$(dirname "$DB")"
uv run python stark.py --db "$DB" run --lan          # terminal 1; allow incoming connections
STARK_SOCKET="$(dirname "$DB")/gateway/stark.sock" \
  "$(../mac/scripts/build-app.sh | tail -1)/Contents/MacOS/StarkMac" &   # this database's StarkMac
uv run --with qrcode==8.0 python stark.py --db "$DB" pair --name "My iPhone"
```

1. Signing: open `StarkPocket.xcodeproj`, StarkPocket target, Signing & Capabilities,
   choose your personal team. Put the iPhone and the Mac on the same Wi-Fi; run the app
   on the iPhone from Xcode.
2. Pair: scan the QR with the iPhone Camera and open it in Stark Pocket; allow local
   network access. Expect "Stark is on Mac".
3. Jack In, talk, Jack Out: tap Jack In (StarkMac dims), send a message, see the reply on
   the phone, tap Jack Out (StarkMac active, same conversation).
4. Drafts both ways: type text in StarkMac without sending, then Jack In on the phone;
   expect the text in the phone's composer, unsent, and StarkMac's composer empty. Type
   text on the phone without sending, then press Jack In Here in StarkMac's menu; expect
   it in StarkMac's composer, unsent, and the chat card not taking the keyboard. Type
   different text in StarkMac first and repeat: expect a Use It / Keep Mine choice, never
   an overwrite. `sqlite3 "$DB" "SELECT draft, draft_state FROM handoffs"` shows each
   carried draft `delivered`; `uv run python stark.py --db "$DB" drafts` lists any that
   are not.
5. Re-pair refused: while paired, open a fresh `stark.py pair` link on the phone. Expect
   "already paired"; the phone stays connected.
6. Unpair while holding: Jack In, then Settings, Unpair This iPhone. Expect "Sending Stark
   home…", StarkMac active, then the pairing screen. Repeat with Wi-Fi off on the phone:
   expect the pairing kept with a recovery message and Forget Anyway.
7. SIGKILL: pair again, Jack In, then `kill -9` the daemon and restart it with the same
   command. Expect the phone to reconnect with Stark still here.
8. Revoke mid jack home: tap Jack Out and run
   `uv run python stark.py --db "$DB" devices revoke <id>` at once. Expect StarkMac to hold
   Stark and the phone to return to pairing, never Stark back on the phone.
9. Stale endpoint: stop the daemon with `kill -9`, then run `stark.py pair` again. Expect a
   refusal, no link.
