"""Gym loadout atlas: accessories follow every frame and nothing else changes.

    uv run --with pillow==11.0.0 --with numpy==2.1.3 --with pytest pytest mac/tools
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))

import compose_loadout as cl

CAP_COLORS = {c[:3] for c in cl.CAP.values()} | {cl.OUTLINE[:3]}
BUD_WHITE = cl.BUD["W"][:3]


@pytest.fixture(scope="module")
def manifest():
    return json.loads((cl.ASSETS / "manifest.json").read_text())


@pytest.fixture(scope="module")
def result():
    return cl.build()


@pytest.fixture(scope="module")
def original(manifest):
    return np.asarray(Image.open(cl.ASSETS / manifest["atlas"]["file"]).convert("RGBA"))


def tiles(manifest, atlas: np.ndarray):
    for f in manifest["frames"]:
        x, y, w, h = f["atlas"]
        yield f["id"], atlas[y:y + h, x:x + w]


def test_every_original_frame_has_an_anchor(result, manifest):
    frames = result.anchors["frames"]
    assert len(manifest["frames"]) == 74
    assert set(frames) == {f["id"] for f in manifest["frames"]}
    for fid, a in frames.items():
        assert a["facing"] in ("front", "left", "right"), fid
        assert set(a["buds"]) <= {"left", "right"} and a["buds"], fid
        x0, y0, x1, y1 = a["face"]
        assert a["head_top"] < y0 < y1 and x0 < x1, fid


def test_the_committed_art_is_the_atlas_jae_approved(result):
    curated = json.loads((cl.ASSETS / "loadout" / "overrides.json").read_text())
    assert result.anchors["approved"] is True
    assert result.anchors["atlas_sha256"] == curated["approved_atlas_sha256"]
    assert result.anchors["approved_by"].startswith("Jae")


def test_approval_comes_only_from_overrides_and_voids_on_new_pixels(tmp_path, manifest):
    for name in ("manifest.json", manifest["atlas"]["file"]):
        shutil.copy(cl.ASSETS / name, tmp_path / name)
    (tmp_path / "loadout").mkdir()
    ov = tmp_path / "loadout" / "overrides.json"
    curated = json.loads((cl.ASSETS / "loadout" / "overrides.json").read_text())
    ov.write_text(json.dumps({"frames": curated["frames"]}))
    assert cl.build(tmp_path).anchors["approved"] is False
    ov.write_text(json.dumps(curated))
    assert cl.build(tmp_path).anchors["approved"] is True
    # Different pixels (one corrected anchor moves the cap) void the approval.
    moved = {**curated, "frames": {**curated["frames"], "r0c0": {"crown_cx": 70}}}
    ov.write_text(json.dumps(moved))
    doc = cl.build(tmp_path).anchors
    assert doc["approved"] is False and "approved_by" not in doc


def test_same_layout_as_the_original_atlas(result, manifest):
    assert result.atlas.size == (manifest["atlas"]["width"], manifest["atlas"]["height"])
    assert result.atlas.mode == "RGBA"


def test_only_accessory_pixels_change(result, manifest, original):
    gym = np.asarray(result.atlas)
    for (fid, before), (_, after), changed in zip(tiles(manifest, original),
                                                   tiles(manifest, gym), result.changed,
                                                   strict=True):
        differs = (before != after).any(-1)
        assert not (differs & ~changed).any(), fid
        # Below the earbuds' reach, the body is the original sprite.
        a = result.anchors["frames"][fid]
        below = a["face"][3] + len(cl.BUD_RIGHT) * cl.BUD_BLOCK
        assert (before[below:] == after[below:]).all(), fid


def test_the_cap_sits_on_the_crown_in_every_frame(result, manifest):
    gym = np.asarray(result.atlas)
    for fid, tile in tiles(manifest, gym):
        a = result.anchors["frames"][fid]
        seam = a["head_top"] + cl.SEAM_BELOW_TOP
        # Just above the seam, at the crown, is cap; the band holds across the dome.
        row = tile[seam - cl.BLOCK - 1]
        assert tuple(row[a["crown_cx"]][:3]) in CAP_COLORS, fid
        cap = np.array([tuple(p[:3]) in CAP_COLORS and p[3] == 255 for p in row])
        xs = np.nonzero(cap)[0]
        mid = (xs.min() + xs.max()) / 2
        if a["facing"] == "front":
            assert abs(mid - a["crown_cx"]) <= 2, fid
        assert xs.max() - xs.min() >= cl.CAP_W * cl.BLOCK - 2 * cl.BLOCK, fid


def test_the_cap_is_never_clipped_by_the_canvas(result, manifest):
    w = manifest["canvas"]["width"]
    for fid, a in result.anchors["frames"].items():
        anchor = cl.Anchor(fid, a["head_top"], a["crown_cx"], tuple(a["face"]), a["facing"],
                           tuple(a["buds"]))
        art, ox, oy = cl.place_cap(anchor, w)
        img = cl.render(art.cells, cl.BLOCK, cl.CAP)
        xs = np.nonzero((img[..., 3] > 0).any(0))[0]
        assert ox + xs.min() >= 0 and ox + xs.max() < w, fid
        assert oy >= 0, fid


def test_a_turned_head_wears_the_bill_toward_its_face(result, manifest):
    gym = np.asarray(result.atlas)
    for fid, tile in tiles(manifest, gym):
        a = result.anchors["frames"][fid]
        if a["facing"] == "front":
            continue
        seam = a["head_top"] + cl.SEAM_BELOW_TOP
        row = tile[seam + 1]
        xs = np.nonzero([tuple(p[:3]) in CAP_COLORS and p[3] == 255 for p in row])[0]
        if a["facing"] == "right":
            assert xs.max() > a["crown_cx"] + cl.CAP_W * cl.BLOCK // 2, fid
        else:
            assert xs.min() < a["crown_cx"] - cl.CAP_W * cl.BLOCK // 2, fid


def test_earbuds_hang_beside_the_face(result, manifest):
    gym = np.asarray(result.atlas)
    for fid, tile in tiles(manifest, gym):
        a = result.anchors["frames"][fid]
        x0, y0, x1, y1 = a["face"]
        white = (tile[..., :3] == BUD_WHITE).all(-1) & (tile[..., 3] == 255)
        for side in a["buds"]:
            band = white[y0:y1 + 1]
            cols = band[:, x1 - 2 * cl.BUD_BLOCK:x1 + 5 * cl.BUD_BLOCK] if side == "right" \
                else band[:, max(x0 - 5 * cl.BUD_BLOCK, 0):x0 + 2 * cl.BUD_BLOCK]
            assert cols.sum() >= 8, (fid, side)


def test_the_starburst_is_gone_above_the_seam(result, manifest):
    gym = np.asarray(result.atlas)
    for fid, tile in tiles(manifest, gym):
        a = result.anchors["frames"][fid]
        seam = a["head_top"] + cl.SEAM_BELOW_TOP
        half = cl.CAP_W * cl.BLOCK // 2
        crown = cl._crown(tile)[:seam, max(a["crown_cx"] - half, 0):a["crown_cx"] + half]
        assert crown.sum() <= 6, (fid, int(crown.sum()))


def test_overrides_win_and_unknown_keys_fail(tmp_path, manifest):
    for name in ("manifest.json", manifest["atlas"]["file"]):
        shutil.copy(cl.ASSETS / name, tmp_path / name)
    (tmp_path / "loadout").mkdir()
    ov = tmp_path / "loadout" / "overrides.json"
    ov.write_text(json.dumps({"frames": {"r0c0": {"facing": "right", "crown_cx": 90}}}))
    a = cl.build(tmp_path).anchors["frames"]["r0c0"]
    assert (a["facing"], a["crown_cx"], a["buds"]) == ("right", 90, ["left"])
    ov.write_text(json.dumps({"frames": {"r0c0": {"tilt": 3}}}))
    with pytest.raises(cl.ComposeError, match="unknown override keys"):
        cl.build(tmp_path)
    ov.write_text(json.dumps({"frames": {"r99c0": {"facing": "front"}}}))
    with pytest.raises(cl.ComposeError, match="unknown frames"):
        cl.build(tmp_path)


def test_output_is_deterministic(result, tmp_path):
    again = cl.build()
    cl.save_png(result.atlas, tmp_path / "a.png")
    cl.save_png(again.atlas, tmp_path / "b.png")
    assert (tmp_path / "a.png").read_bytes() == (tmp_path / "b.png").read_bytes()
    assert again.anchors == result.anchors


def test_committed_outputs_are_current(result, tmp_path):
    cl.save_png(result.atlas, tmp_path / "gym.png")
    assert (tmp_path / "gym.png").read_bytes() == (cl.ASSETS / cl.GYM_ATLAS).read_bytes(), \
        "rerun mac/tools/compose_loadout.py"
    committed = json.loads((cl.ASSETS / "loadout" / "anchors.json").read_text())
    assert committed == json.loads(json.dumps(result.anchors))
