"""Bake Stark's gym loadout (black cap, white earbuds) onto every original frame.

    uv run --with pillow==11.0.0 --with numpy==2.1.3 python mac/tools/compose_loadout.py

The original atlas, manifest, and source sheet are inputs only; nothing here rewrites
them. Output is a second atlas with the same layout, so frame N of stark-atlas-gym.png is
frame N of stark-atlas.png with accessories added, and every animation plays unchanged.

Anchors, per frame, from the pixels and then loadout/overrides.json:

- head_top: the first opaque row.
- crown_cx: the mean x of the dark brown starburst on top of the hair, in the band
  CROWN_BAND rows below head_top. The cap centers on it.
- face: the bounding box of the largest run of skin pixels (rows of skin connected
  vertically) above the torso. The earbuds hang beside it.
- facing: front, left, or right. Proposed from where the face sits within the hair mass;
  overrides.json holds the curated value for frames where the proposal is wrong.

Accessories are pixel art drawn on a BLOCK pixel grid, then scaled by nearest neighbor,
so they share the chunky texture and thick dark outline of the original sprite. Colors
come from the sprite's own near black outline and a neutral grey ramp for the cap, and a
white ramp for the earbuds.

The cap replaces the starburst: crown pixels under or above the cap's band are cleared,
or filled with nearby hair where the hair continues, before the cap is drawn. Pixels
outside each frame's accessory mask stay byte identical to the original atlas.

Outputs in Assets/stark: stark-atlas-gym.png, loadout/anchors.json (deterministic),
and preview-gym.png (each animation, standard above gym, for review).

Approval: overrides.json carries Jae's approval and the sha256 of the atlas reviewed.
anchors.json reports approved only while the composed atlas still has that digest.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parents[1] / "StarkMac" / "Assets" / "stark"
GYM_ATLAS = "stark-atlas-gym.png"
BLOCK = 3              # canvas pixels per accessory pixel, matching the sprite's texture
BUD_BLOCK = 2          # earbuds are small; a finer grid keeps them readable
CROWN_BAND = 60        # rows below head_top searched for the starburst
FACE_MIN_ROW = 70      # the face never starts above this many rows below head_top... floor
FRONT_DX = 9           # |face center - hair center| at or under this reads as front

# Cap geometry in blocks.
CAP_W = 31             # dome width
CAP_H = 12             # dome height above the seam
BRIM_FRONT = 5         # brim depth seen from the front, below the seam
BRIM_SIDE = 10         # brim reach past the dome toward the facing side
SEAM_BELOW_TOP = 49    # canvas rows from head_top to the cap's seam

OUTLINE = (14, 10, 10, 255)
CAP = {"S": (18, 18, 21, 255), "D": (28, 28, 33, 255), "M": (40, 40, 47, 255),
       "H": (58, 58, 68, 255), "L": (84, 84, 96, 255), "B": (70, 70, 80, 255)}
SHADES = "SDMHL"                    # dark to light
LIGHT = (-0.55, -0.65, 0.52)        # from the upper left, toward the viewer
BUD = {"W": (246, 246, 244, 255), "G": (196, 200, 206, 255), "K": (40, 40, 46, 255)}

# Earbud, BUD_BLOCK grid, for the right side of the face (the left one is mirrored): the
# rounded head points at the ear canal (toward the face), the stem hangs down.
BUD_RIGHT = (
    ".KKK.",
    "KWWWK",
    "KWWGK",
    "KWGK.",
    ".KWK.",
    ".KWK.",
    ".KGK.",
    "..K..",
)


class ComposeError(Exception):
    pass


@dataclass(frozen=True)
class Anchor:
    id: str
    head_top: int
    crown_cx: int
    face: tuple[int, int, int, int]   # x0, y0, x1, y1 inclusive
    facing: str                        # front | left | right
    buds: tuple[str, ...]              # which sides show an earbud: left, right

    def as_json(self) -> dict:
        return {"head_top": self.head_top, "crown_cx": self.crown_cx,
                "face": list(self.face), "facing": self.facing, "buds": list(self.buds)}


# ---------------------------------------------------------------------- masks
def _crown(f: np.ndarray) -> np.ndarray:
    r, g, b, a = (f[..., i].astype(int) for i in range(4))
    rf = np.maximum(r, 1)
    return (a > 128) & (r >= 40) & (r <= 150) & (g >= rf // 4) & (g * 100 <= rf * 62) & \
        (b <= g + 4)


def _skin(f: np.ndarray) -> np.ndarray:
    r, g, b, a = (f[..., i].astype(int) for i in range(4))
    return (a > 128) & (r > 225) & (g > 165) & (b > 115) & (r - b > 35) & (r - b < 130)


def _opaque(f: np.ndarray) -> np.ndarray:
    return f[..., 3] > 128


def _face_box(skin: np.ndarray, top: int) -> tuple[int, int, int, int]:
    """The widest band of skin rows below the hair: the face, not a hand or an arm."""
    rows = skin.sum(1)
    lo = top + FACE_MIN_ROW
    best: tuple[int, int] | None = None
    y = lo
    while y < skin.shape[0]:
        if rows[y] >= 6:
            start = y
            while y < skin.shape[0] and rows[y] >= 6:
                y += 1
            if best is None or rows[start:y].sum() > rows[best[0]:best[1]].sum():
                best = (start, y)
        y += 1
    if best is None:
        raise ComposeError("no face found")
    y0, y1 = best
    xs = np.nonzero(skin[y0:y1].any(0))[0]
    return int(xs.min()), y0, int(xs.max()), y1 - 1


def propose(fid: str, f: np.ndarray) -> Anchor:
    op = _opaque(f)
    top = int(np.argmax(op.any(1)))
    crown = _crown(f)
    band = crown[top:top + CROWN_BAND]
    ys, xs = np.nonzero(band)
    if len(xs) < 40:
        raise ComposeError(f"{fid}: no crown found")
    crown_cx = int(round(float(np.median(xs))))
    face = _face_box(_skin(f), top)
    # Hair center: middle of the opaque extent over the face rows.
    mids = []
    for y in range(face[1], face[3] + 1):
        xo = np.nonzero(op[y])[0]
        if len(xo):
            mids.append((xo.min() + xo.max()) / 2)
    hair_cx = float(np.median(mids))
    dx = (face[0] + face[2]) / 2 - hair_cx
    facing = "front" if abs(dx) <= FRONT_DX else ("right" if dx > 0 else "left")
    buds = {"front": ("left", "right"), "right": ("left",), "left": ("right",)}[facing]
    return Anchor(fid, top, crown_cx, face, facing, buds)


def anchors(manifest: dict, atlas: np.ndarray, overrides: dict) -> list[Anchor]:
    out = []
    for fr in manifest["frames"]:
        x, y, w, h = fr["atlas"]
        a = propose(fr["id"], atlas[y:y + h, x:x + w])
        o = overrides.get(fr["id"], {})
        unknown = set(o) - {"head_top", "crown_cx", "facing", "buds", "note"}
        if unknown:
            raise ComposeError(f"{fr['id']}: unknown override keys {sorted(unknown)}")
        if "facing" in o and o["facing"] not in ("front", "left", "right"):
            raise ComposeError(f"{fr['id']}: facing must be front, left, or right")
        facing = o.get("facing", a.facing)
        default_buds = {"front": ("left", "right"), "right": ("left",),
                        "left": ("right",)}[facing]
        out.append(Anchor(a.id, o.get("head_top", a.head_top), o.get("crown_cx", a.crown_cx),
                          a.face, facing, tuple(o.get("buds", default_buds))))
    return out


# ---------------------------------------------------------------------- pixel art
def _grow(mask: np.ndarray) -> np.ndarray:
    """8 neighbor dilation by one cell."""
    p = np.pad(mask, 1)
    out = np.zeros_like(mask)
    h, w = mask.shape
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            out |= p[dy:dy + h, dx:dx + w]
    return out


@dataclass(frozen=True)
class CapArt:
    cells: list[str]
    dome_cx: float      # dome center, in canvas pixels from the image's left edge
    seam: int           # seam row, in canvas pixels from the image's top edge


def cap_cells(facing: str, reach: int = BRIM_SIDE) -> CapArt:
    """The cap on the BLOCK grid as rows of palette keys ('.' clear, 'K' outline). reach
    is how far a turned head's bill sticks out past the dome, in blocks."""
    side = 0 if facing == "front" else reach
    pad = 2                                   # room for the outline and a front brim lip
    w = CAP_W + side + 2 * pad
    h = CAP_H + BRIM_FRONT + 2 * pad
    seam = pad + CAP_H                        # the row where dome meets brim
    dome_x0 = pad if facing != "left" else pad + side
    cx = dome_x0 + (CAP_W - 1) / 2
    rx, ry = CAP_W / 2, CAP_H
    grid = [["." for _ in range(w)] for _ in range(h)]
    body = np.zeros((h, w), bool)
    # Panel seams run from the button to the band: the center seam and one each side,
    # turned toward the facing side when the head is turned.
    turn = {"front": 0.0, "right": 0.35, "left": -0.35}[facing]
    seams = [turn, turn - 0.55, turn + 0.55]
    for yy in range(h):
        for xx in range(w):
            nx, ny = (xx - cx) / rx, (yy + 0.5 - seam) / ry
            if ny > 0 or nx * nx + ny * ny > 1.0:
                continue
            body[yy, xx] = True
            nz = (max(0.0, 1.0 - nx * nx - ny * ny)) ** 0.5
            lit = nx * LIGHT[0] + ny * LIGHT[1] + nz * LIGHT[2]
            level = 1 + int(lit > 0.25) + int(lit > 0.55) + int(lit > 0.8)
            if ny > -0.09:
                level = 1                     # the band just above the seam
            across = (1.0 - ny * ny) ** 0.5   # half width of the dome at this row
            if any(abs(nx - s_ * across) < 0.5 / rx for s_ in seams) and ny < -0.2:
                level = max(level - 1, 0)
            grid[yy][xx] = SHADES[level]
    if facing == "front":
        # Seen from the front and a little above: a visor wider than the dome, curving
        # down toward the viewer, its top face lit in the middle and its rim in shadow.
        brx, bry = CAP_W / 2 + 1.5, BRIM_FRONT + 0.5
        for yy in range(seam, seam + BRIM_FRONT):
            for xx in range(w):
                nx, ny = (xx - cx) / brx, (yy + 0.5 - seam) / bry
                if nx * nx + ny * ny > 1.0:
                    continue
                body[yy, xx] = True
                rim = (nx * nx + ((yy + 1.5 - seam) / bry) ** 2) >= 1.0
                if rim:
                    grid[yy][xx] = "S"
                else:
                    grid[yy][xx] = "L" if abs(nx) < 0.35 else ("H" if abs(nx) < 0.8 else "M")
    else:
        # Turned: a bill reaching toward the facing side, thinning at its tip.
        for i, yy in enumerate(range(seam, seam + 3)):
            if facing == "right":
                xs = range(dome_x0 + CAP_W // 3, dome_x0 + CAP_W + side - (0, 1, 3)[i])
            else:
                xs = range(dome_x0 - side + (0, 1, 3)[i], dome_x0 + CAP_W - CAP_W // 3)
            for xx in xs:
                body[yy, xx] = True
                grid[yy][xx] = ("M", "D", "S")[i]
    # Seam line and a button on top.
    for xx in range(w):
        if body[seam - 1, xx]:
            grid[seam - 1][xx] = "D"
    top_row = int(np.nonzero(body.any(1))[0][0])
    grid[top_row][int(round(cx))] = "B"
    ring = _grow(body) & ~body
    for yy, xx in zip(*np.nonzero(ring), strict=True):
        grid[yy][xx] = "K"
    return CapArt(["".join(r) for r in grid], (dome_x0 + CAP_W / 2) * BLOCK, seam * BLOCK)


def bud_cells(side: str) -> list[str]:
    return list(BUD_RIGHT) if side == "right" else [r[::-1] for r in BUD_RIGHT]


def render(cells: list[str], block: int, palette: dict) -> np.ndarray:
    h, w = len(cells), len(cells[0])
    out = np.zeros((h * block, w * block, 4), np.uint8)
    for yy, row in enumerate(cells):
        for xx, key in enumerate(row):
            if key == ".":
                continue
            color = OUTLINE if key == "K" and palette is CAP else palette[key]
            out[yy * block:(yy + 1) * block, xx * block:(xx + 1) * block] = color
    return out


# ---------------------------------------------------------------------- compositing
def _paste(dst: np.ndarray, src: np.ndarray, x0: int, y0: int) -> np.ndarray:
    """Alpha paste src (opaque or clear pixels) at x0, y0; returns the mask written."""
    h, w = dst.shape[:2]
    mask = np.zeros((h, w), bool)
    sy0, sx0 = max(0, -y0), max(0, -x0)
    dy0, dx0 = max(0, y0), max(0, x0)
    sy1 = min(src.shape[0], h - y0)
    sx1 = min(src.shape[1], w - x0)
    if sy1 <= sy0 or sx1 <= sx0:
        return mask
    s = src[sy0:sy1, sx0:sx1]
    on = s[..., 3] > 0
    region = dst[dy0:dy0 + s.shape[0], dx0:dx0 + s.shape[1]]
    region[on] = s[on]
    mask[dy0:dy0 + s.shape[0], dx0:dx0 + s.shape[1]] = on
    return mask


def reach_for(a: Anchor, canvas_w: int) -> int:
    """The longest bill, up to BRIM_SIDE blocks, that stays inside the canvas."""
    half = CAP_W * BLOCK / 2
    room = (canvas_w - (a.crown_cx + half)) if a.facing == "right" else (a.crown_cx - half)
    return int(max(3, min(BRIM_SIDE, room // BLOCK - 2)))


def cap_origin(a: Anchor, art: CapArt) -> tuple[int, int]:
    """Canvas position of the cap image: the dome centered on crown_cx, the seam
    SEAM_BELOW_TOP rows under head_top."""
    return int(round(a.crown_cx - art.dome_cx)), a.head_top + SEAM_BELOW_TOP - art.seam


def place_cap(a: Anchor, canvas_w: int) -> tuple[CapArt, int, int]:
    """The cap for a frame and its origin, nudged sideways only as far as it takes to
    keep a bill near the canvas edge whole."""
    art = cap_cells(a.facing, reach_for(a, canvas_w))
    ox, oy = cap_origin(a, art)
    cols = [x for x in range(len(art.cells[0])) if any(r[x] != "." for r in art.cells)]
    lo, hi = cols[0] * BLOCK, (cols[-1] + 1) * BLOCK
    ox = max(ox, -lo)
    ox = min(ox, canvas_w - hi)
    return art, ox, oy


def bud_origin(a: Anchor, side: str, bud: np.ndarray) -> tuple[int, int]:
    x0, y0, x1, y1 = a.face
    y = y0 + (y1 - y0) // 5
    if side == "right":
        return x1 - BUD_BLOCK, y
    return x0 - bud.shape[1] + BUD_BLOCK, y


def compose(frame: np.ndarray, a: Anchor) -> tuple[np.ndarray, np.ndarray]:
    """The gym frame and the mask of every pixel it changed."""
    out = frame.copy()
    art, ox, oy = place_cap(a, frame.shape[1])
    cap = render(art.cells, BLOCK, CAP)
    seam_y = oy + art.seam
    # Clear the starburst above the seam that the cap does not cover.
    crown = _crown(frame)
    crown[seam_y:] = False
    crown[:max(a.head_top - 2, 0)] = False
    covered = _paste(np.zeros_like(frame), cap, ox, oy)
    # Above the seam and within the cap's span, anything that is not red hair (the
    # starburst and its outline) goes; red spikes stay and frame the cap.
    span = np.zeros(crown.shape, bool)
    xs = np.nonzero(covered.any(0))[0]
    span[:seam_y, max(xs.min() - BLOCK, 0):xs.max() + BLOCK + 1] = True
    stray = (_grow(_grow(crown)) | (span & ~_hair(frame))) & ~covered & (frame[..., 3] > 0)
    stray[seam_y:] = False
    stray &= ~_hair(frame) | _crown(frame)
    out[stray] = 0
    changed = stray.copy()
    changed |= _paste(out, cap, ox, oy)
    for side in a.buds:
        bud = render(bud_cells(side), BUD_BLOCK, BUD)
        bx, by = bud_origin(a, side, bud)
        changed |= _paste(out, bud, bx, by)
    return out, changed


def _hair(f: np.ndarray) -> np.ndarray:
    r, g, b, a = (f[..., i].astype(int) for i in range(4))
    return (a > 128) & (r > 150) & (g < 140)


# ---------------------------------------------------------------------- build
@dataclass(frozen=True)
class Result:
    atlas: Image.Image
    anchors: dict
    preview: Image.Image
    changed: list[np.ndarray]


def build(assets: Path = ASSETS) -> Result:
    manifest = json.loads((assets / "manifest.json").read_text())
    src = Image.open(assets / manifest["atlas"]["file"]).convert("RGBA")
    atlas = np.asarray(src).copy()
    ov_path = assets / "loadout" / "overrides.json"
    curated = json.loads(ov_path.read_text()) if ov_path.exists() else {}
    overrides = curated.get("frames", {})
    unknown = set(overrides) - {f["id"] for f in manifest["frames"]}
    if unknown:
        raise ComposeError(f"overrides name unknown frames {sorted(unknown)}")
    anc = anchors(manifest, atlas, overrides)
    out = atlas.copy()
    changed = []
    for fr, a in zip(manifest["frames"], anc, strict=True):
        x, y, w, h = fr["atlas"]
        g, m = compose(atlas[y:y + h, x:x + w], a)
        out[y:y + h, x:x + w] = g
        changed.append(m)
    gym = Image.fromarray(out, "RGBA")
    digest = atlas_sha256(gym)
    # Approval is Jae's, recorded in overrides.json against the exact atlas reviewed. Any
    # change to the composed pixels changes the digest and voids it.
    approved = curated.get("approved") is True and curated.get("approved_atlas_sha256") == digest
    doc = {
        "version": 1,
        "atlas": GYM_ATLAS,
        "atlas_sha256": digest,
        "approved": approved,
        "block": BLOCK,
        "frames": {a.id: a.as_json() for a in anc},
    }
    if approved and curated.get("approved_by"):
        doc["approved_by"] = curated["approved_by"]
    return Result(gym, doc, preview(manifest, src, gym), changed)


def preview(manifest: dict, std: Image.Image, gym: Image.Image) -> Image.Image:
    """Each animation as two rows, standard then gym, at half size like preview.png."""
    cw, ch = manifest["canvas"]["width"] // 2, manifest["canvas"]["height"] // 2
    by_id = {f["id"]: f for f in manifest["frames"]}
    anims = sorted(manifest["animations"].items())
    longest = max(len(v["frames"]) for _, v in anims)
    label_w = 96
    row_h = ch + 14
    img = Image.new("RGBA", (label_w + longest * cw, len(anims) * 2 * row_h + 8),
                    (40, 44, 52, 255))
    draw = ImageDraw.Draw(img)
    for i, (name, anim) in enumerate(anims):
        for k, (sheet, tag) in enumerate(((std, "default"), (gym, "gym"))):
            y = (2 * i + k) * row_h
            draw.text((6, y + ch // 2 - 6), f"{name}\n{tag}", fill=(240, 240, 240, 255))
            for j, fid in enumerate(anim["frames"]):
                x0, y0, w, h = by_id[fid]["atlas"]
                x = label_w + j * cw
                draw.rectangle((x, y, x + cw - 1, y + ch - 1),
                               fill=(60, 66, 78, 255) if k == 0 else (66, 74, 66, 255))
                tile = sheet.crop((x0, y0, x0 + w, y0 + h))
                img.alpha_composite(tile.resize((cw, ch), Image.Resampling.NEAREST), (x, y))
                draw.text((x + 2, y + ch + 1), fid, fill=(170, 170, 170, 255))
    return img


def atlas_sha256(img: Image.Image) -> str:
    """The digest of the PNG bytes save_png writes for img."""
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False, compress_level=9)
    return hashlib.sha256(buf.getvalue()).hexdigest()


def save_png(img: Image.Image, path: Path) -> None:
    img.save(path, format="PNG", optimize=False, compress_level=9)


def write(result: Result, assets: Path = ASSETS) -> None:
    save_png(result.atlas, assets / GYM_ATLAS)
    save_png(result.preview, assets / "preview-gym.png")
    (assets / "loadout").mkdir(exist_ok=True)
    (assets / "loadout" / "anchors.json").write_text(
        json.dumps(result.anchors, indent=1, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--assets", type=Path, default=ASSETS)
    args = ap.parse_args(argv)
    try:
        result = build(args.assets)
    except ComposeError as e:
        print(f"compose_loadout: {e}", file=sys.stderr)
        return 1
    write(result, args.assets)
    print(f"{len(result.anchors['frames'])} frames composed into {GYM_ATLAS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
