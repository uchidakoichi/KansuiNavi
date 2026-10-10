"""水域のマスク（標高タイルの穴を DEM10B で埋めるときに水面を除くため。2026-09-30）。

★国土地理院 最適化ベクトルタイルの PMTiles（手元。通信しない）の `WA`（水域）レイヤを、
  主系列（z15）のタイルの 256×256 の格子に描く。z15 のタイル1枚は z16 の4枚に当たる。
★DEM5A は川・池などの水面を欠損にしている。そこを DEM10B で埋めると、水面の標高で Δh が変わり、
  「10mメッシュで推定」の印が水辺の道路に付く（7回目のレビュー）。水面は欠損のまま残す。
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

import numpy as np

HALF = 20037508.342789244   # Web メルカトルの半周［m］


def _merc(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    n = 2**z
    return (-HALF + 2 * HALF * x / n, HALF - 2 * HALF * (y + 1) / n,
            -HALF + 2 * HALF * (x + 1) / n, HALF - 2 * HALF * y / n)


class WaterMask:
    """`mask(x, y)` は主系列 z のタイル (x, y) の水域（True）。PMTiles は z = z+1 を読む。"""

    def __init__(self, pmtiles_path: Path, z: int = 15, layer: str = "WA") -> None:
        from kansui.pmtiles import PMTiles

        self.pm = PMTiles(Path(pmtiles_path))
        self.z = z
        self.layer = layer
        self._cached = lru_cache(maxsize=4096)(self._mask)

    def mask(self, x: int, y: int) -> np.ndarray:
        return self._cached(x, y)

    def _mask(self, x: int, y: int) -> np.ndarray:
        import mapbox_vector_tile as mvt
        import shapely
        from rasterio.features import rasterize
        from rasterio.transform import from_bounds

        zc = self.z + 1
        polys = []
        for dx in (0, 1):
            for dy in (0, 1):
                cx, cy = 2 * x + dx, 2 * y + dy
                data = self.pm.fetch(zc, cx, cy)
                if not data:
                    continue
                lyr = mvt.decode(data).get(self.layer)
                if not lyr:
                    continue
                ext = lyr.get("extent", 4096)
                w, s, e, n = _merc(cx, cy, zc)

                def m(p, w=w, s=s, e=e, n=n, ext=ext):
                    # mapbox_vector_tile は既定で y を下から上へ直して返す
                    return (w + (e - w) * p[0] / ext, s + (n - s) * p[1] / ext)

                for f in lyr["features"]:
                    g = f["geometry"]
                    cs = g["coordinates"]
                    rings = [cs] if g["type"] == "Polygon" else cs if g["type"] == "MultiPolygon" else []
                    for poly in rings:
                        if len(poly[0]) < 3:
                            continue
                        polys.append(shapely.Polygon([m(p) for p in poly[0]],
                                                     [[m(p) for p in h] for h in poly[1:] if len(h) >= 3]))
        if not polys:
            return np.zeros((256, 256), dtype=bool)
        tr = from_bounds(*_merc(x, y, self.z), 256, 256)
        return rasterize([(p, 1) for p in polys if not p.is_empty], out_shape=(256, 256),
                         transform=tr, fill=0).astype(bool)


def _check_merc() -> None:  # pragma: no cover  簡単な自己点検（東京駅が入る z15 タイル）
    lon, lat = 139.7671, 35.6812
    n = 2**15
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    w, s, e, nn = _merc(x, y, 15)
    mx = lon * HALF / 180
    assert w <= mx <= e
