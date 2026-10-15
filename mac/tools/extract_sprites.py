"""Extract Stark's frames from the source sprite sheet into a stable atlas and manifest.

    uv run --with pillow==11.0.0 --with numpy==2.1.3 python mac/tools/extract_sprites.py

Detection reads only the alpha channel and assumes no grid: empty pixel rows split the
sheet into bands, empty columns split a band into sprites, and a fragment narrower than
MIN_SPRITE_W (a detached strand or sparkle) joins its nearest neighbor. The detected
counts per band must equal groups.json, or the tool fails before writing anything.

Alignment, onto one canvas anchor at Stark's feet:

- vertical: one translation per band, the band's median feet row (lowest opaque row), so a
  hop keeps its authored lift and a standing pose never bobs;
- horizontal: per sprite, the median x of its torso band (TORSO rows above the feet).
  Sprites sit at arbitrary sheet positions, and the torso stays put while arms gesture and
  legs stride, so walking reads as walking in place under a steady body.

Pixels are copied byte exact; only position changes.

Outputs, beside groups.json: stark-atlas.png, manifest.json (deterministic, sorted keys),
and preview.png (a labeled contact sheet for review). --frames DIR also writes one PNG per
sprite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parents[1] / "StarkMac" / "Assets" / "stark"
SOURCE = "source/stark-spritesheet.png"
ALPHA_MIN = 8          # fainter pixels are antialias haze: ignored for detection, still copied
MIN_GAP = 2            # empty rows or columns that separate bands or sprites
MIN_SPRITE_W = 24      # narrower column runs are fragments of a neighbor
TORSO = (62, 24)       # torso band: from 62 to 24 rows above the feet
MARGIN = 4
ATLAS_COLUMNS = 8
REQUIRED = ("idle", "blink", "listening", "thinking", "working", "notify", "walkLeft",
            "walkRight", "sleep", "error")

Box = tuple[int, int, int, int]   # x0, y0, x1, y1, half open


class ExtractionError(Exception):
    pass


@dataclass(frozen=True)
class Result:
    atlas: Image.Image
    manifest: dict
    tiles: list[Image.Image]
    preview: Image.Image


def runs(mask: np.ndarray, min_gap: int) -> list[tuple[int, int]]:
    """Half open runs of True, joining runs separated by fewer than min_gap False."""
    out: list[list[int]] = []
    for i in np.flatnonzero(mask).tolist():
        if out and i - out[-1][1] < min_gap:
            out[-1][1] = i + 1
        else:
            out.append([i, i + 1])
    return [(a, b) for a, b in out]


def merge_fragments(cols: list[tuple[int, int]]) -> list[tuple[int, int]]:
    cols = list(cols)
    while len(cols) > 1:
        narrow = [i for i, (a, b) in enumerate(cols) if b - a < MIN_SPRITE_W]
        if not narrow:
            break
        i = narrow[0]
        gap_left = cols[i][0] - cols[i - 1][1] if i > 0 else None
        gap_right = cols[i + 1][0] - cols[i][1] if i + 1 < len(cols) else None
        j = i - 1 if gap_right is None or (gap_left is not None and gap_left <= gap_right) \
            else i + 1
        lo, hi = min(i, j), max(i, j)
        cols[lo:hi + 1] = [(cols[lo][0], cols[hi][1])]
    return cols


def detect(alpha: np.ndarray) -> list[list[Box]]:
    """Sprite boxes grouped by band, top to bottom, each band left to right."""
    solid = alpha > ALPHA_MIN
    rows = []
    for y0, y1 in runs(solid.any(axis=1), MIN_GAP):
        band = solid[y0:y1]
        boxes = []
        for x0, x1 in merge_fragments(runs(band.any(axis=0), MIN_GAP)):
            ys = np.flatnonzero(band[:, x0:x1].any(axis=1))
            boxes.append((x0, y0 + int(ys[0]), x1, y0 + int(ys[-1]) + 1))
        rows.append(boxes)
    return rows


def grow(box: Box, alpha: np.ndarray) -> Box:
    """Extend a box over faint pixels touching it, so haze below ALPHA_MIN is kept."""
    x0, y0, x1, y1 = box
    h, w = alpha.shape
    changed = True
    while changed:
        changed = False
        if x0 > 0 and alpha[y0:y1, x0 - 1].any():
            x0, changed = x0 - 1, True
        if x1 < w and alpha[y0:y1, x1].any():
            x1, changed = x1 + 1, True
        if y0 > 0 and alpha[y0 - 1, x0:x1].any():
            y0, changed = y0 - 1, True
        if y1 < h and alpha[y1, x0:x1].any():
            y1, changed = y1 + 1, True
    return x0, y0, x1, y1


def feet_row(alpha: np.ndarray, box: Box) -> int:
    x0, y0, x1, y1 = box
    solid = alpha[y0:y1, x0:x1] > ALPHA_MIN
    return y0 + int(np.flatnonzero(solid.any(axis=1))[-1])


def torso_x(alpha: np.ndarray, box: Box, feet_y: int) -> int:
    x0, _, x1, _ = box
    band = alpha[feet_y - TORSO[0]:feet_y - TORSO[1], x0:x1] > ALPHA_MIN
    xs = np.nonzero(band)[1]
    return x0 + int(np.median(xs))


def build(assets: Path = ASSETS) -> Result:
    groups = json.loads((assets / "groups.json").read_text())
    src = assets / SOURCE
    sheet = Image.open(src)
    if sheet.mode != "RGBA":
        raise ExtractionError(f"{SOURCE} must be RGBA, found {sheet.mode}")
    alpha = np.asarray(sheet)[:, :, 3]
    rows = [[grow(b, alpha) for b in r] for r in detect(alpha)]
    counts = [len(r) for r in rows]
    if counts != groups["rows"]:
        raise ExtractionError(f"detected sprites per row {counts}; groups.json says "
                              f"{groups['rows']}. Fix the sheet or the mapping.")
    flat = [b for r in rows for b in r]
    for i, a in enumerate(flat):
        for b in flat[i + 1:]:
            if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                raise ExtractionError(f"sprite boxes {a} and {b} overlap")

    # Reference point per sprite: its torso x, and its band's median feet row.
    refs = []
    for r in rows:
        fy = int(statistics.median_low(feet_row(alpha, b) for b in r))
        refs.append([(torso_x(alpha, b, feet_row(alpha, b)), fy) for b in r])
    rel = [(b[0] - fx, b[1] - fy, b[2] - fx, b[3] - fy)
           for r, rr in zip(rows, refs, strict=True)
           for b, (fx, fy) in zip(r, rr, strict=True)]
    anchor = (MARGIN - min(b[0] for b in rel), MARGIN - min(b[1] for b in rel))
    canvas = (max(b[2] for b in rel) + anchor[0] + MARGIN,
              max(b[3] for b in rel) + anchor[1] + MARGIN)

    frames, tiles = [], []
    for ri, (r, rr) in enumerate(zip(rows, refs, strict=True)):
        for ci, ((x0, y0, x1, y1), (fx, fy)) in enumerate(zip(r, rr, strict=True)):
            dx, dy = anchor[0] - fx, anchor[1] - fy
            tile = Image.new("RGBA", canvas, (0, 0, 0, 0))
            tile.paste(sheet.crop((x0, y0, x1, y1)), (x0 + dx, y0 + dy))
            i = len(frames)
            frames.append({"id": f"r{ri}c{ci}", "row": ri, "col": ci,
                           "source": [x0, y0, x1 - x0, y1 - y0],
                           "offset": [x0 + dx, y0 + dy],
                           "atlas": [(i % ATLAS_COLUMNS) * canvas[0],
                                     (i // ATLAS_COLUMNS) * canvas[1], *canvas]})
            tiles.append(tile)

    atlas_rows = -(-len(tiles) // ATLAS_COLUMNS)
    atlas = Image.new("RGBA", (ATLAS_COLUMNS * canvas[0], atlas_rows * canvas[1]),
                      (0, 0, 0, 0))
    for f, t in zip(frames, tiles, strict=True):
        atlas.paste(t, tuple(f["atlas"][:2]))

    animations = {}
    for name, spec in sorted(groups["animations"].items()):
        ids = []
        for r, c in spec["frames"]:
            if r >= len(rows) or c >= len(rows[r]):
                raise ExtractionError(f"{name} names missing sprite row {r} col {c}")
            ids.append(f"r{r}c{c}")
        anim = {"frames": ids, "fps": spec["fps"], "loop": spec["loop"]}
        for key in ("loop_from", "next", "fallback"):
            if key in spec:
                anim[key] = spec[key]
        animations[name] = anim
    missing = [n for n in REQUIRED if n not in animations]
    if missing:
        raise ExtractionError(f"groups.json lacks required animations {missing}")

    manifest = {
        "version": 1,
        "source": {"file": SOURCE, "sha256": hashlib.sha256(src.read_bytes()).hexdigest(),
                   "width": sheet.size[0], "height": sheet.size[1]},
        "atlas": {"file": "stark-atlas.png", "width": atlas.size[0],
                  "height": atlas.size[1], "columns": ATLAS_COLUMNS},
        "canvas": {"width": canvas[0], "height": canvas[1]},
        "anchor": {"x": anchor[0], "y": anchor[1]},
        "frames": frames,
        "animations": animations,
    }
    return Result(atlas, manifest, tiles, preview(frames, tiles, animations, canvas))


def preview(frames, tiles, animations, canvas) -> Image.Image:
    """Contact sheet: one row per animation, frames at half size with ids, anchor marked."""
    scale = 2
    cw, ch = canvas[0] // scale, canvas[1] // scale
    by_id = dict(zip((f["id"] for f in frames), tiles, strict=True))
    longest = max(len(a["frames"]) for a in animations.values())
    label_w = 110
    img = Image.new("RGBA", (label_w + longest * cw, len(animations) * (ch + 14)),
                    (40, 44, 52, 255))
    draw = ImageDraw.Draw(img)
    for row, (name, anim) in enumerate(sorted(animations.items())):
        y = row * (ch + 14)
        draw.text((6, y + ch // 2), name, fill=(240, 240, 240, 255))
        for i, fid in enumerate(anim["frames"]):
            x = label_w + i * cw
            draw.rectangle((x, y, x + cw - 1, y + ch - 1), fill=(60, 66, 78, 255))
            tile = by_id[fid].resize((cw, ch), Image.Resampling.NEAREST)
            img.alpha_composite(tile, (x, y))
            draw.text((x + 2, y + ch + 1), fid, fill=(170, 170, 170, 255))
    return img


def save_png(img: Image.Image, path: Path) -> None:
    # No metadata and fixed compression: identical input gives identical bytes.
    img.save(path, format="PNG", optimize=False, compress_level=9)


def write(result: Result, assets: Path = ASSETS, frames_dir: Path | None = None) -> None:
    save_png(result.atlas, assets / "stark-atlas.png")
    save_png(result.preview, assets / "preview.png")
    (assets / "manifest.json").write_text(
        json.dumps(result.manifest, indent=1, sort_keys=True) + "\n")
    if frames_dir is not None:
        frames_dir.mkdir(parents=True, exist_ok=True)
        for f, t in zip(result.manifest["frames"], result.tiles, strict=True):
            save_png(t, frames_dir / f"{f['id']}.png")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--assets", type=Path, default=ASSETS)
    ap.add_argument("--frames", type=Path, default=None, help="also write one PNG per sprite")
    args = ap.parse_args(argv)
    try:
        result = build(args.assets)
    except ExtractionError as e:
        print(f"extract_sprites: {e}", file=sys.stderr)
        return 1
    write(result, args.assets, args.frames)
    m = result.manifest
    print(f"{len(m['frames'])} frames, canvas {m['canvas']['width']}x{m['canvas']['height']}, "
          f"anchor {m['anchor']['x']},{m['anchor']['y']}, {len(m['animations'])} animations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
