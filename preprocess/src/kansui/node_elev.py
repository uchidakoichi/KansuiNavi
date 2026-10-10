"""ノードの標高（F-03 ルート標高プロファイル用）。

★**手元にキャッシュされた標高タイルだけを読む。通信しない。**
  地域の作成（build_region）が Δh のために取った標高タイルがそのまま使える。
  ★作成の連鎖が地理院のサーバを使っているあいだに並行で動かしてよいように、
    取得器（TileFetcher）を使わずにキャッシュのファイルを直接読む（絶対ルール3）。

★主系列（DEM5A）から順に試し、どれも無ければ NaN（**推定値で埋めない**。T-09）。
★DEM10B は 2026-09-28 の NK-07 決定（「ないよりはまし」）から**使う**。どの種別から取ったかを
  返すので、呼び出し側でプロファイルに「精度の低い区間」として出し分ける（破線）。

★2026-09-24、国土地理院から「標高タイルは申請不要・出典の明示で利用可」と回答があり、
  配布物に標高値を入れてよくなった（藤沢版 CLAUDE.md の絶対ルール11 の解除）。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import numpy as np

from kansui.dem_png import decode_dem_png
from kansui.tiles import TILE_SIZE


def _cache_file(cache_dir: Path, url_template: str, z: int, x: int, y: int) -> Path:
    # ★TileFetcher._cache_path と同じ規則（URL のパスをそのままディレクトリにする）
    return cache_dir / urlparse(url_template.format(z=z, x=x, y=y)).path.lstrip("/")


def _pixels(lon: np.ndarray, lat: np.ndarray, z: int) -> tuple[np.ndarray, np.ndarray]:
    """経緯度 → その z の全体画素座標（Web メルカトル）。"""
    n = 2.0**z * TILE_SIZE
    px = (lon + 180.0) / 360.0 * n
    s = np.sin(np.radians(lat))
    py = (0.5 - np.log((1 + s) / (1 - s)) / (4 * np.pi)) * n
    return px, py


def sample(lon: np.ndarray, lat: np.ndarray, cache_dir: Path, chain,
           with_source: bool = False):
    """点ごとの標高［m］（float32、取れなければ NaN）。

    `chain` は `kansui.dem.DemSource` の列（先頭が主系列）。
    `with_source=True` なら (標高, 取った種別の chain 上の番号・取れなければ -1) を返す。
    """
    lon = np.asarray(lon, dtype=np.float64)
    lat = np.asarray(lat, dtype=np.float64)
    out = np.full(len(lon), np.nan, dtype=np.float32)
    which = np.full(len(lon), -1, dtype=np.int8)
    todo = np.arange(len(lon))
    for si, src in enumerate(chain):
        if not len(todo):
            break
        px, py = _pixels(lon[todo], lat[todo], src.zoom)
        tx = np.floor(px / TILE_SIZE).astype(np.int64)
        ty = np.floor(py / TILE_SIZE).astype(np.int64)
        ix = np.clip(np.floor(px - tx * TILE_SIZE).astype(np.int64), 0, TILE_SIZE - 1)
        iy = np.clip(np.floor(py - ty * TILE_SIZE).astype(np.int64), 0, TILE_SIZE - 1)
        key = tx * (1 << 32) + ty
        order = np.argsort(key, kind="stable")
        ks = key[order]
        cut = np.flatnonzero(np.diff(ks)) + 1
        for grp in np.split(order, cut):
            x, y = int(tx[grp[0]]), int(ty[grp[0]])
            f = _cache_file(cache_dir, src.url, src.zoom, x, y)
            if not f.exists():
                continue
            arr = decode_dem_png(f.read_bytes())
            out[todo[grp]] = arr[iy[grp], ix[grp]]
            got = todo[grp][~np.isnan(out[todo[grp]])]
            which[got] = si
        todo = todo[np.isnan(out[todo])]
    return (out, which) if with_source else out
