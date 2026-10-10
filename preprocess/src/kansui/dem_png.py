"""標高タイル(PNG形式)の復号。

★藤沢版（`../../Kansui/preprocess/src/kansui/dem_png.py`）から**そのまま**持ってきた。
  T-08 / T-09 の対策そのものなので、意図的に同じにしてある。片方だけ直さないこと。


公式仕様 https://maps.gsi.go.jp/development/demtile.html

    d = R·2^16 + G·2^8 + B
    d < 2^23  → h = d · u
    d = 2^23  → h = NA（無効値。RGB = (128, 0, 0)）
    d > 2^23  → h = (d − 2^24) · u
    u = 0.01 [m]

★ T-08: `− 2^24` を絶対に落とさないこと。
   落とすと標高0m未満（海面下・地下）が巨大な正の値になり、
   「最も低い場所」を探す冠水判定が根本から壊れる。藤沢は沿岸低地を含むため現実に踏む。

★ T-09: RGB(128,0,0) は「欠損」であって「標高0m」ではない。
   0m と誤読すると水部・未整備域が「最も低い＝最も危険」と誤判定される。
   ここでは np.nan を返し、呼び出し側が次の DEM 種別へフォールバックする。
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

ELEVATION_RESOLUTION_M = 0.01
_POW2_16 = 1 << 16
_POW2_8 = 1 << 8
_POW2_23 = 1 << 23
_POW2_24 = 1 << 24


def decode_dem_png(data: bytes) -> np.ndarray:
    """標高タイル PNG を float32 の標高配列 [m] に復号する。欠損は np.nan。"""
    with Image.open(io.BytesIO(data)) as img:
        rgb = np.asarray(img.convert("RGB"), dtype=np.int64)

    if rgb.shape[:2] != (256, 256):
        raise ValueError(f"標高タイルは 256x256 のはずですが {rgb.shape[:2]} でした")

    d = rgb[..., 0] * _POW2_16 + rgb[..., 1] * _POW2_8 + rgb[..., 2]

    # 負の標高（海面下・地下）。ここを落とすと冠水判定が壊れる。
    h = np.where(d < _POW2_23, d, d - _POW2_24).astype(np.float64) * ELEVATION_RESOLUTION_M

    # d == 2^23 が無効値。RGB(128,0,0) と厳密に等価（128 * 2^16 == 2^23）。
    h[d == _POW2_23] = np.nan
    return h.astype(np.float32)


def nodata_ratio(elevation: np.ndarray, mask: np.ndarray | None = None) -> float:
    """欠損率。mask を渡すとその範囲内だけで計算する（市域ピクセルに限定するため）。"""
    if mask is None:
        total = elevation.size
        missing = int(np.isnan(elevation).sum())
    else:
        total = int(mask.sum())
        if total == 0:
            return float("nan")
        missing = int(np.isnan(elevation[mask]).sum())
    return missing / total if total else float("nan")
