"""Extraction correctness for Stark's sprite atlas.

    uv run --with pillow==11.0.0 --with numpy==2.1.3 --with pytest pytest mac/tools
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))

import extract_sprites as ex

STILL = ("idle", "blink", "listening", "thinking", "working", "sleep", "error")


@pytest.fixture(scope="module")
def result():
    return ex.build()


@pytest.fixture(scope="module")
def source():
    return np.asarray(Image.open(ex.ASSETS / ex.SOURCE))


def frames_by_id(result):
    return {f["id"]: f for f in result.manifest["frames"]}


def test_detects_every_sprite_per_row(result):
    groups = json.loads((ex.ASSETS / "groups.json").read_text())
    rows = [0] * len(groups["rows"])
    for f in result.manifest["frames"]:
        rows[f["row"]] += 1
    assert rows == groups["rows"] and sum(rows) == 74


def test_bounds_inside_source_and_disjoint(result, source):
    h, w = source.shape[:2]
    boxes = [f["source"] for f in result.manifest["frames"]]
    for x, y, bw, bh in boxes:
        assert 0 <= x and 0 <= y and x + bw <= w and y + bh <= h
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert not (a[0] < b[0] + b[2] and b[0] < a[0] + a[2]
                        and a[1] < b[1] + b[3] and b[1] < a[1] + a[3])


def test_every_visible_source_pixel_belongs_to_a_frame(result, source):
    covered = np.zeros(source.shape[:2], dtype=bool)
    for x, y, bw, bh in (f["source"] for f in result.manifest["frames"]):
        covered[y:y + bh, x:x + bw] = True
    orphans = (source[:, :, 3] > 0) & ~covered
    assert int(orphans.sum()) == 0, "pixels outside every frame: a sprite was cut"


def test_pixels_and_alpha_copied_byte_exact(result, source):
    atlas = np.asarray(result.atlas)
    for f in result.manifest["frames"]:
        x, y, bw, bh = f["source"]
        ax, ay = f["atlas"][:2]
        ox, oy = f["offset"]
        got = atlas[ay + oy:ay + oy + bh, ax + ox:ax + ox + bw]
        assert np.array_equal(got, source[y:y + bh, x:x + bw]), f["id"]
        tile = atlas[ay:ay + f["atlas"][3], ax:ax + f["atlas"][2]]
        outside = tile[:, :, 3].copy()
        outside[oy:oy + bh, ox:ox + bw] = 0
        assert not outside.any(), f"{f['id']} has stray pixels outside its sprite"


def test_canvas_edges_are_transparent(result):
    atlas = np.asarray(result.atlas)
    for f in result.manifest["frames"]:
        ax, ay, cw, ch = f["atlas"]
        tile = atlas[ay:ay + ch, ax:ax + cw, 3]
        m = ex.MARGIN
        assert not tile[:m].any() and not tile[-m:].any()
        assert not tile[:, :m].any() and not tile[:, -m:].any()


def test_still_animations_share_a_baseline(result):
    by_id = frames_by_id(result)
    atlas_alpha = np.asarray(result.atlas)[:, :, 3]
    anchor_y = result.manifest["anchor"]["y"]
    for name in STILL:
        for fid in result.manifest["animations"][name]["frames"]:
            ax, ay, cw, ch = by_id[fid]["atlas"]
            rows = np.flatnonzero((atlas_alpha[ay:ay + ch, ax:ax + cw] > ex.ALPHA_MIN).any(axis=1))
            assert abs(int(rows[-1]) - anchor_y) <= 1, f"{name} {fid} feet at {rows[-1]}"


def test_hop_keeps_its_authored_lift(result, source):
    # The hop row's feet rise in the source; the atlas keeps that rise, frame for frame.
    by_id = frames_by_id(result)
    hop = [by_id[f"r4c{c}"] for c in range(5)]
    src_feet = [f["source"][1] + f["source"][3] for f in hop]
    atlas_feet = [f["offset"][1] + f["source"][3] for f in hop]
    assert len(set(src_feet)) > 1
    assert [a - s for a, s in zip(atlas_feet, src_feet, strict=True)] == \
        [atlas_feet[0] - src_feet[0]] * 5


def test_output_is_deterministic(result, tmp_path):
    again = ex.build()
    assert json.dumps(again.manifest, sort_keys=True) == json.dumps(result.manifest, sort_keys=True)
    ex.save_png(result.atlas, tmp_path / "a.png")
    ex.save_png(again.atlas, tmp_path / "b.png")
    assert (tmp_path / "a.png").read_bytes() == (tmp_path / "b.png").read_bytes()


def test_committed_outputs_are_current(result, tmp_path):
    ex.save_png(result.atlas, tmp_path / "atlas.png")
    assert (tmp_path / "atlas.png").read_bytes() == (ex.ASSETS / "stark-atlas.png").read_bytes(), \
        "rerun mac/tools/extract_sprites.py"
    committed = json.loads((ex.ASSETS / "manifest.json").read_text())
    assert committed == json.loads(json.dumps(result.manifest))


def test_required_animations_and_fallbacks_documented(result):
    anims = result.manifest["animations"]
    for name in ex.REQUIRED:
        assert anims[name]["frames"]
    assert "fallback" in anims["working"] and "fallback" in anims["error"]


def test_mismatched_counts_fail_loudly(tmp_path):
    (tmp_path / "source").mkdir()
    (tmp_path / "source" / "stark-spritesheet.png").write_bytes(
        (ex.ASSETS / ex.SOURCE).read_bytes())
    groups = json.loads((ex.ASSETS / "groups.json").read_text())
    groups["rows"][0] += 1
    (tmp_path / "groups.json").write_text(json.dumps(groups))
    with pytest.raises(ex.ExtractionError, match="detected sprites per row"):
        ex.build(tmp_path)
