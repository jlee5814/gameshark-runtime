"""Cut Stark's gym still out of its painted source and fit it to the sprite canvas.

    uv run --with pillow==11.0.0 python mac/tools/extract_gym.py

The source is one pose on a blurred dark corridor, not a sheet, so detection cannot read
alpha. It reads the pixel art outline instead: near black pixels (every channel at most
OUTLINE) about one art pixel thick. Steps:

- drop near black speckle in the corridor with a small opening (real outlines survive);
- bridge outline gaps (hair spikes) with a BRIDGE dilation and flood the background from
  the border, then flood once more through the halo band the dilation left, stopping at
  the true outline;
- keep the component under the image center, after an opening cuts thin necks to stray
  corridor patches;
- scale with nearest neighbor so Stark is as tall as the idle frame (capped at the canvas
  width) and put his feet on the manifest anchor, centered on it.

Output, beside manifest.json: stark-gym.png (canvas size, RGBA). The manifest is not
touched: extract_sprites.py rewrites it, and StarkKit finds the still by name
(Outfit.gymFile).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ASSETS = Path(__file__).resolve().parent.parent / "StarkMac" / "Assets" / "stark"
SOURCE = "source/stark-gym-source.png"
OUTPUT = "stark-gym.png"
OUTLINE = 10      # channel ceiling for outline pixels
SPECKLE = 5       # opening that drops corridor noise
BRIDGE = 31       # dilation that closes outline gaps
NECK = 15         # opening that cuts stray patches from the sprite
RESTORE = 17      # dilation that gives the sprite back its edges after NECK


@dataclass
class Result:
    still: Image.Image
    cutout_box: tuple[int, int, int, int]
    scale: float
    source_sha256: str


def _near_black(im: Image.Image) -> Image.Image:
    r, g, b = im.split()
    dark = lambda c: c.point(lambda v: 255 if v <= OUTLINE else 0)
    return ImageChops.multiply(ImageChops.multiply(dark(r), dark(g)), dark(b))


def _from_border(through: Image.Image) -> Image.Image:
    """Pixels reachable from the image border through `through` (255), as 255."""
    w, h = through.size
    m = through.point(lambda v: 0 if v else 64)
    seeds = [(x, y) for x in range(0, w, 8) for y in (0, h - 1)]
    seeds += [(x, y) for y in range(0, h, 8) for x in (0, w - 1)]
    for p in seeds:
        if m.getpixel(p) == 0:
            ImageDraw.floodfill(m, p, 255)
    return m.point(lambda v: 255 if v == 255 else 0)


def cutout(im: Image.Image) -> Image.Image:
    """The sprite's mask: 255 on Stark, 0 on the corridor."""
    w, h = im.size
    outline = _near_black(im)
    outline = outline.filter(ImageFilter.MinFilter(SPECKLE)).filter(ImageFilter.MaxFilter(SPECKLE))
    outside = _from_border(ImageChops.invert(outline.filter(ImageFilter.MaxFilter(BRIDGE))))
    band = outside.filter(ImageFilter.MaxFilter(BRIDGE))
    background = _from_border(ImageChops.multiply(band, ImageChops.invert(outline)))
    fg = ImageChops.invert(background)
    opened = fg.filter(ImageFilter.MinFilter(NECK)).filter(ImageFilter.MaxFilter(NECK))
    comp = opened.point(lambda v: 0 if v else 64)
    center = (w // 2, int(h * 0.55))
    if comp.getpixel(center) != 0:
        raise SystemExit("extract_gym: the image center is not on the sprite")
    ImageDraw.floodfill(comp, center, 255)
    core = comp.point(lambda v: 255 if v == 255 else 0)
    return ImageChops.multiply(fg, core.filter(ImageFilter.MaxFilter(RESTORE)))


def build() -> Result:
    data = (ASSETS / SOURCE).read_bytes()
    im = Image.open(ASSETS / SOURCE).convert("RGB")
    rgba = im.convert("RGBA")
    rgba.putalpha(cutout(im))
    box = rgba.getbbox()
    sprite = rgba.crop(box)

    m = json.loads((ASSETS / "manifest.json").read_text())
    atlas = Image.open(ASSETS / m["atlas"]["file"]).convert("RGBA")
    idle_id = m["animations"]["idle"]["frames"][0]
    x, y, fw, fh = next(f for f in m["frames"] if f["id"] == idle_id)["atlas"]
    idle = atlas.crop((x, y, x + fw, y + fh)).getbbox()
    cw, ch = m["canvas"]["width"], m["canvas"]["height"]
    ax, ay = m["anchor"]["x"], m["anchor"]["y"]
    scale = min((idle[3] - idle[1]) / sprite.height, (cw - 4) / sprite.width)
    sw, sh = round(sprite.width * scale), round(sprite.height * scale)
    still = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    still.alpha_composite(sprite.resize((sw, sh), Image.NEAREST),
                          (max(0, min(cw - sw, ax - sw // 2)), ay + 1 - sh))
    return Result(still, box, scale, hashlib.sha256(data).hexdigest())


def main() -> None:
    r = build()
    r.still.save(ASSETS / OUTPUT, optimize=True)
    print(f"{OUTPUT}: {r.still.size[0]}x{r.still.size[1]}, cutout {r.cutout_box}, "
          f"scale {r.scale:.4f}, source sha256 {r.source_sha256}")


if __name__ == "__main__":
    main()
