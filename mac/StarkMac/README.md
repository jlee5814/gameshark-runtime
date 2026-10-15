# StarkMac

The first visible Stark surface: a floating, draggable companion on the macOS desktop. It
renders the GameShark runtime's state and sends the user's messages; it owns no agent
state. Identity, presence, responsibilities, and the conversation all live in the runtime
(`prototype/`), reached over an authenticated Unix socket. Design and invariants:
[architecture/stark-companion.md](../../architecture/stark-companion.md).

## Build and run

Requires macOS 14 or later and Swift 6 (Xcode or Command Line Tools).

```sh
cd prototype
uv run python stark.py run                       # serves ~/.gameshark/stark.sock
../mac/scripts/build-app.sh                      # mac/build/StarkMac.app
open ../mac/build/StarkMac.app
```

Stark appears at the bottom right. Click him to open the chat; drag him anywhere (the
position persists). The menu bar item offers Show Chat, Click Through, Reconnect, Save
Snapshot, and Quit.

| Variable | Effect |
|---|---|
| `STARK_SOCKET` | gateway socket (default `~/.gameshark/stark.sock`) |
| `STARK_TOKEN` | token file (default `gateway.token` beside the socket) |
| `STARK_CLICK_THROUGH=1` | start with click through on |
| `STARK_SNAPSHOT_DIR` | write a PNG and a `states.log` line whenever what Stark shows changes |
| `STARK_DEMO_CONTROL=1` | accept `starkctl` commands (demo only) |

`open` does not pass variables; run `StarkMac.app/Contents/MacOS/StarkMac` directly to
use them.

For a live model, start the runtime with `--inference-url http://127.0.0.1:30000/v1
--model <name>` (SGLang or any OpenAI compatible server). Without it, a deterministic
local fake answers after `--fake-delay` seconds.

## Avatar states

| State | When | Look |
|---|---|---|
| disconnected | no live session | gray, eyes closed, still |
| ghost | another surface holds presence | faded gray; chat read only |
| working | Stark's reply is in flight | indigo, orbiting sparks, visor sweep |
| listening | the chat field has focus | teal, wide eyes, pulse rings |
| notification | a reply arrived unviewed | orange, bouncing badge |
| idle | otherwise | blue, breathing, blinking |

## Click through

Click Through sets `ignoresMouseEvents`, so clicks reach the windows beneath Stark and a
dashed ring marks the mode. Turn it off from the menu bar item; Show Chat also turns it
off.

## Tests and demo

```sh
mac/scripts/swiftpm.sh test                                # unit and fake runtime tests
STARK_INTEGRATION=1 mac/scripts/swiftpm.sh test --filter IntegrationTests   # real daemon
mac/scripts/demo.sh                                        # seven acceptance checks
mac/scripts/demo.sh --interactive                          # plus a real mouse drag
```

`demo.sh` uses `~/.gameshark/demo`, never the everyday database, and writes
`acceptance.md` plus snapshots there. Synthetic mouse events need Accessibility
permission, so the automated run moves the panel through `starkctl`, which calls the same
code a finished drag calls; `--interactive` asks for a real drag and verifies it.

## Troubleshooting

- **Gray and disconnected:** the runtime is not serving. Check `stark.py run` logs for
  `gateway listening`; a socket path over 103 bytes disables the gateway.
- **`token: tooOpen`:** `chmod 600 ~/.gameshark/gateway.token`.
- **Manifest fails to link under Command Line Tools:** some installs keep a stale Swift
  5.10 `PackageDescription` private interface. `mac/scripts/swiftpm.sh` detects it and
  applies a build local VFS overlay; plain `swift build` will fail on such machines.
