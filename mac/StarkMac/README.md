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

Stark appears at the bottom right. Click him to open or close the chat, double click to
open it, drag him anywhere (he walks the way you pull him), and right click for Avatar
Scale (1×, 2×, 3×), the Animation Gallery, Cycle Demo, connection status, Click Through,
Reconnect, and Save Snapshot. The menu bar item shows the same menu, so it stays
reachable while click through is on. Position and scale persist in the app's own
UserDefaults (`dev.gameshark.starkmac`), never in the runtime database.

| Variable | Effect |
|---|---|
| `STARK_SOCKET` | gateway socket (default `~/.gameshark/stark.sock`) |
| `STARK_TOKEN` | token file (default `gateway.token` beside the socket) |
| `STARK_CLICK_THROUGH=1` | start with click through on |
| `STARK_SNAPSHOT_DIR` | write a PNG and a `states.log` line whenever what Stark shows changes |
| `STARK_DEMO_CONTROL=1` | accept `starkctl` commands (demo only) |
| `STARK_AVATAR_DEMO=1` | cycle through every animation, no runtime needed |
| `STARK_SPRITES` | directory holding `manifest.json` and the atlas (development) |

`open` does not pass variables; run `StarkMac.app/Contents/MacOS/StarkMac` directly to
use them.

For a live model, start the runtime with `--inference-url http://127.0.0.1:30000/v1
--model <name>` (SGLang or any OpenAI compatible server). Without it, a deterministic
local fake answers after `--fake-delay` seconds.

## Stark's sprites

Stark's body comes from his sprite sheet, never redrawn:

```
Assets/stark/source/stark-spritesheet.png   original sheet, committed untouched
Assets/stark/groups.json                    which sprites form which animation, and timing
Assets/stark/stark-atlas.png                extracted frames on one shared canvas
Assets/stark/manifest.json                  frame rects, anchor, animations (generated)
Assets/stark/preview.png                    labeled contact sheet for review (generated)
```

Regenerate after replacing the sheet or editing `groups.json`:

```sh
uv run --with pillow==11.0.0 --with numpy==2.1.3 python mac/tools/extract_sprites.py
uv run --with pillow==11.0.0 --with numpy==2.1.3 --with pytest pytest mac/tools
```

**Detection** reads alpha only and assumes no grid: empty pixel rows split the sheet into
bands, empty columns split a band into sprites, and slivers narrower than 24 px (a loose
strand or sparkle) join their neighbor. Faint antialias pixels touching a sprite stay with
it. The tool refuses to write anything unless the counts per band equal `groups.json`.

**Alignment** puts every frame on one canvas (163×198 here) with Stark's feet at the same
anchor: vertically by each band's median foot row, so a hop keeps its lift; horizontally
by each sprite's torso, which holds still while arms gesture and legs stride. Pixels are
copied byte exact; tests prove it, and prove no visible source pixel was left out.

**Grouping** lives in `groups.json`: each animation lists `[band, sprite]` pairs, `fps`,
`loop`, and optional `loop_from` (notify hops once, then loops its wave). To change timing,
edit `fps` and rerun. To add an animation, add an entry, add a case to `StarkAnimation`
(StarkKit), and map it in `StarkAnimation.base(for:)` or play it with `playOnce`.

| Animation | Sprites | Runtime trigger |
|---|---|---|
| idle | band 0, standing | nothing active |
| blink, lookAround | band 0 closed eyes; band 9 glances | every 3.5 to 7 s while idle |
| listening | band 8, attentive nod | chat field focused |
| thinking | band 7, hand to chin | Stark's reply inference in flight |
| working | band 6, busy gestures (fallback: no tool pose exists) | none yet; gallery only |
| notify | band 4 hop, then band 3 wave | reply arrived, not yet viewed |
| walkRight, walkLeft | bands 1 and 2 | dragged right or left |
| sleep | band 5, dozing | runtime disconnected, or 10 min quiet |
| error | band 10, head shake (fallback: no distress pose exists) | a responsibility is blocked |

Precedence: gallery override, then walking, then a one shot (blink), then the runtime
base. A surface without presence draws idle, faded. Animation only reads runtime state; it
never writes identity, presence, duties, or inference. One timer runs at frame rate (3 to
12 Hz) and pauses while the panel is hidden; idle costs about 0.5 percent of one core.
Frames render with nearest neighbor sampling at an integer device pixel scale, so 1× is
one sheet pixel per screen pixel. Without an atlas, the vector avatar draws instead.

## Click through

Click Through sets `ignoresMouseEvents`, so clicks reach the windows beneath Stark and a
dashed mark under Stark shows the mode. Turn it off from the menu bar item; Show Chat also turns it
off.

## Tests and demo

```sh
mac/scripts/swiftpm.sh test                                # unit and fake runtime tests
STARK_INTEGRATION=1 mac/scripts/swiftpm.sh test --filter IntegrationTests   # real daemon
mac/scripts/demo.sh                                        # eight acceptance checks
mac/scripts/demo.sh --interactive                          # plus a real mouse drag
```

`demo.sh` uses `~/.gameshark/demo`, never the everyday database, and writes
`acceptance.md` plus snapshots there, one per animation in `shots/gallery`. Synthetic
mouse events need Accessibility permission, so the automated run moves the panel through
`starkctl`, which calls the same code a finished drag calls; `--interactive` asks for a
real drag and verifies it.

## Troubleshooting

- **Gray and disconnected:** the runtime is not serving. Check `stark.py run` logs for
  `gateway listening`; a socket path over 103 bytes disables the gateway.
- **`token: tooOpen`:** `chmod 600 ~/.gameshark/gateway.token`.
- **Manifest fails to link under Command Line Tools:** some installs keep a stale Swift
  5.10 `PackageDescription` private interface. `mac/scripts/swiftpm.sh` detects it and
  applies a build local VFS overlay; plain `swift build` will fail on such machines.
