"""NK-07: DEM10B（z14）を z15 の枠へ貼るときの切り出し位置と拡大。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np

spec = importlib.util.spec_from_file_location(
    "br", Path(__file__).resolve().parents[1] / "tools" / "build_region.py")
br = importlib.util.module_from_spec(spec)
spec.loader.exec_module(br)


def test_quadrant_matches_child_tile():
    # z14 のタイルを4象限に分けて値を変える: 左上 1 / 右上 2 / 左下 3 / 右下 4
    a = np.zeros((256, 256), np.float32)
    a[:128, :128], a[:128, 128:], a[128:, :128], a[128:, 128:] = 1, 2, 3, 4
    # z15 の子 (2x+i, 2y+j) は、列 i・行 j の象限
    for (cx, cy), want in {(10, 20): 1, (11, 20): 2, (10, 21): 3, (11, 21): 4}.items():
        out = br.upsample_part(a, cx, cy, 1)
        assert out.shape == (256, 256)
        assert (out == want).all(), (cx, cy)


def test_nearest_does_not_smear_nan():
    """★欠損を周りへにじませない（最近傍）。"""
    a = np.ones((256, 256), np.float32)
    a[0, 0] = np.nan
    out = br.upsample_part(a, 0, 0, 1)
    assert np.isnan(out[:2, :2]).all() and np.isfinite(out[2:, :]).all()
