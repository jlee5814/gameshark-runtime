# Stark avatar loadout (Phase 4.4B)

Stark wears a black cap and white earbuds while the user is at the gym, on StarkMac and
Stark Pocket alike. The loadout is costume: it changes which atlas a surface draws and
nothing else. No tool, memory, inference request, persona, speech, presence, or audio
route reads it. Avatar earbuds say nothing about the user's Bluetooth route, and no code
path inspects one.

## Authority

The runtime owns the loadout (`gameshark/loadout.py`, table `avatar_loadout`, one row per
identity). Surfaces render the snapshot's value and `loadout` pushes; they never infer it
from rows they hold.

Two inputs, in precedence order:

1. **Manual override** (gym or default): `loadout_set` from any authenticated surface,
   `/gym` or `/default` in either composer, the StarkMac Loadout menu, or
   `stark.py loadout gym|default`. It pins the loadout until cleared with Auto
   (`/auto`, the menu, `stark.py loadout auto`, `override: null`).
2. **Inferred context**: the newest user message that explicitly says the user is at, or
   leaving, the gym.

With neither, the loadout is default. While a pin holds, inference still records the
context underneath, so Auto returns to where the user said they are.

| Command | Kind | Effect |
|---|---|---|
| `loadout_set {op_id, override: gym\|default\|null}` | write | pin or clear; lease guarded, no presence fence; a resent op_id returns the current row, a reused one with another value is `invalid` |

The snapshot carries `loadout: {effective, inferred, override, inferred_seq}` and
`features` lists `loadout`. A change from either input pushes a `loadout` event after
`messages` and `memories`, before `activity`. A surface that reconnects reads the
snapshot, so a change made while it was away is never lost to a missed push.

Why no presence fence: the pin is costume, not a conversation write, so the phone may
pin while Stark sits on the Mac, as forget works for memory.

## Inference

`SurfaceConversation.submit` calls `infer_in_txn` right after it inserts the user row, in
the same write transaction. So the context lands with its message or not at all, a
resent `client_msg_id` (a dedup hit returns before the insert) never infers twice, and a
send refused by presence or the lease infers nothing. It runs after submission and before
any reply, whether or not the model answers.

Detection is narrow by design (`detect` in `loadout.py`):

- Enter: first person, present tense, the gym named: `I'm at the gym`, `I am in the gym`,
  `I just got to the gym`, `I'm here at the gym`.
- Exit: `I'm leaving the gym`, `I left the gym`, `just left the gym`,
  `I'm done at the gym`, `I'm back from the gym`.
- Ignored: any clause with a negation (`not`, `n't`, `never`, `no longer`), a
  hypothetical (`if`, `when`, `would`, `maybe`, `imagine`, `pretend`), a future or plan
  (`will`, `'ll`, `going to`, `gonna`, `later`, `tomorrow`, `soon`, `in 10 minutes`), a
  habit (`usually`, `always`), a question mark, quoted text, other people, and near
  words (`Gymshark`, `gymnastics`, `working out`, `heading to the gym`).
- The last qualifying clause in a message wins. Assistant rows never count.

Paraphrases miss on purpose; the override covers them.

## Art

`mac/tools/compose_loadout.py` bakes the accessories onto every one of the 74 original
frames and writes `stark-atlas-gym.png` with the same layout as `stark-atlas.png`.
Renderers keep one manifest and one animation controller and swap only the frame images,
so every animation (idle, blink, listening, thinking, working, notify, walks, sleep,
error, lookAround) plays unchanged in either loadout. The original atlas, manifest, and
source sheet are never rewritten.

Per frame anchors (`Assets/stark/loadout/anchors.json`, generated):

- `head_top`: the first opaque row.
- `crown_cx`: the median x of the dark starburst on top of the hair; the cap centers here.
- `face`: the box of the widest band of skin rows; the earbuds hang beside it.
- `facing`: front, left, or right, proposed from where the face sits in the hair mass.
- `buds`: which sides show an earbud (both from the front, the near ear when turned).

`Assets/stark/loadout/overrides.json` holds hand corrections (two frames today: a dozing
tilt and a hand at the chin read as turns). The cap is drawn on a 3 pixel grid with the
sprite's thick dark outline and a five tone grey ramp lit from the upper left, with panel
seams and a visor seen from the front or a bill toward a turned face. Under the cap the
starburst is cleared; red spikes stay and frame it. A bill near the canvas edge shortens
or nudges inward so it is never clipped. Earbuds use a 2 pixel grid.

Tests (`mac/tools/test_compose_loadout.py`) check 74 anchors, the same atlas size, no
changed pixel outside each frame's accessory mask, the cap on the crown and inside the
canvas in every frame, the bill toward the face, earbuds beside the face, no starburst
left above the seam, overrides, determinism, and committed outputs.

**Approval gate**: `anchors.json` carries `"approved": false` until Jae accepts the
gallery. The renderers ship the atlas regardless, so the physical check can run; the
flag records review, not a runtime switch.

## Surfaces

- StarkKit: `Loadout`, `LoadoutState`, `LoadoutCommand`, `AtlasCrop`, `LoadoutFrames`;
  `ClientState.loadout`, `loadoutOp` (a pin shows at once and goes out when live; a
  fenced or draining refusal resends the same op; any other refusal reverts), and
  `shownLoadout`. A dropped session keeps the last value; a new identity resets it.
- StarkMac: `SpriteStage` draws the gym frames when `shownLoadout` is gym; the right
  click and status menu gain Loadout (Auto, Default, Gym) and Preview Gym Loadout (a
  local gallery preview that never asks the runtime). `starkctl loadout gym|default|auto`
  and `starkctl avatar gym|standard|runtime` drive the same paths for demos.
- Stark Pocket: `PocketStage` does the same; `stark-atlas-gym.png` is bundled beside the
  standard atlas. The accessibility label is unchanged on Pocket (it lives in a view
  Phase 4.4A is editing).
