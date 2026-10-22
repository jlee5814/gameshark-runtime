"""Gym still correctness.

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

import extract_gym as gym


@pytest.fixture(scope="module")
def result():
    return gym.build()


@pytest.fixture(scope="module")
def manifest():
    return json.loads((gym.ASSETS / "manifest.json").read_text())


def test_fits_the_sprite_canvas(result, manifest):
    assert result.still.mode == "RGBA"
    assert result.still.size == (manifest["canvas"]["width"], manifest["canvas"]["height"])


def test_feet_rest_on_the_anchor(result, manifest):
    alpha = np.asarray(result.still)[:, :, 3]
    rows = np.nonzero(alpha.any(axis=1))[0]
    cols = np.nonzero(alpha.any(axis=0))[0]
    assert rows.max() == manifest["anchor"]["y"]
    assert abs((cols.min() + cols.max()) / 2 - manifest["anchor"]["x"]) <= 3


def test_corridor_is_gone(result):
    alpha = np.asarray(result.still)[:, :, 3]
    h, w = alpha.shape
    for y, x in [(0, 0), (0, w - 1), (h - 1, 0), (h - 1, w - 1), (5, 5), (h // 2, 0)]:
        assert alpha[y, x] == 0
    # Only Stark is left: one opaque block of a sensible share of the canvas.
    share = (alpha > 0).mean()
    assert 0.35 < share < 0.75


def test_stark_is_opaque_where_he_stands(result, manifest):
    alpha = np.asarray(result.still)[:, :, 3]
    ax, ay = manifest["anchor"]["x"], manifest["anchor"]["y"]
    assert alpha[ay - 60, ax] == 255          # torso
    assert alpha[ay - 120, ax] == 255         # face


def test_the_committed_still_matches_the_tool(result):
    committed = np.asarray(Image.open(gym.ASSETS / gym.OUTPUT).convert("RGBA"))
    assert np.array_equal(committed, np.asarray(result.still))
