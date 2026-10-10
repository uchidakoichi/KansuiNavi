"""エッジのサンプリング、標高付与、Δh と凹地判定。

★藤沢版（`../../Kansui/preprocess/src/kansui/terrain.py`）を一般化したもの。
  違いは **EPSG を引数で受ける**こと（藤沢版は 6677 を直書きして検査していた）。

★★**T-12 はここでは起きない。** 全画素に円形 `minimum_filter` をかけるのではなく、
  **サンプル点でのみ**窓を切り出して正確な円の最小値を取るため。
  八角形近似も不要。`config.yaml` に `footprint` を置く必要も無い。

要件定義書 2.1 の定義:
    Δh(p) = h(p) − min{ h(q) | q ∈ N(p, r) }     （r = 100m / 250m の2スケール）
    Δh ≈ 0 かつ 近傍が閉じている（周囲が全方位で高い）→ 凹地候補

★近傍の形は「円」。全画素ではなく**サンプル点でのみ**計算することで正確な円が使える。
  全画素で円形フットプリントの minimum_filter を回すと分離可能でなくなり破綻する
  （Step 0 で 1m格子・半径257px を試して OOM になった）。
  一方サンプル点は 20m 間隔で十数万点しかなく、各点で窓を切り出して円マスクの最小値を
  取れば正確かつ現実的な計算量に収まる。近似（正方・八角形）を入れる必要がない。

★「近傍が閉じている」の実装
  各方位について、中心から半径 r までの光線上の**最高標高（縁の高さ）**を求め、
  その全方位にわたる最小値と h(p) の差を「閉じ込み深さ」とする。
      enclosure(p) = min_over_azimuths( max_along_ray(h) ) − h(p)
  これが正なら全方位が縁で囲まれている＝水が抜けない。負またはゼロなら、
  その方位へ水が流れ出せる。要件定義書の「周囲が全方位で高い」をそのまま式にしたもの。

★距離はすべて**その地域の平面直角座標系**（メートル）で扱う。
  EPSG:3857 では緯度35度で22%ずれる（T-05）。
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass

import numpy as np
import rasterio
from shapely.geometry import LineString

log = logging.getLogger(__name__)


def sample_line(line: LineString, interval_m: float) -> np.ndarray:
    """LineString を等間隔でサンプリングして (N, 2) の座標配列を返す。

    始点と終点は必ず含める。長さが interval 未満なら両端のみ。
    """
    length = line.length
    if length <= 0:
        p = line.coords[0]
        return np.array([[p[0], p[1]]], dtype=np.float64)
    n = max(int(np.floor(length / interval_m)), 1)
    dists = np.linspace(0.0, length, n + 1)
    return np.array([[p.x, p.y] for p in (line.interpolate(d) for d in dists)])


def sample_edges(edges, interval_m: float):
    """全エッジを等間隔サンプリングし、(座標(N,2), 所属エッジ位置(N,)) を返す。

    ★P-03 と検証側で同じ点列を使うための唯一の実装。
      別々に書くと、点の並びがずれた瞬間に samples_p04.parquet の行と
      座標の対応が崩れ、**エラーを出さずに検証結果だけが狂う。**
      edges は EPSG:6677 であること（interval_m がメートルのため）。
    """
    coords, owner = [], []
    for i, geom in enumerate(edges.geometry.to_numpy()):
        if geom is None or geom.is_empty:
            continue
        parts = geom.geoms if geom.geom_type == "MultiLineString" else [geom]
        for part in parts:
            pts = sample_line(part, interval_m)
            coords.append(pts)
            owner.append(np.full(len(pts), i, dtype=np.int64))
    return np.vstack(coords), np.concatenate(owner)


@dataclass
class Neighborhood:
    """円形近傍と方位別光線のインデックスを、半径ごとに一度だけ作って使い回す。"""

    radius_px: int
    disk: np.ndarray          # (2r+1, 2r+1) bool
    ray_index: np.ndarray     # (n_az, radius_px) 窓内フラットインデックス

    @classmethod
    def build(cls, radius_m: float, resolution_m: float, n_azimuths: int = 32) -> Neighborhood:
        r = int(round(radius_m / resolution_m))
        if r < 1:
            raise ValueError(f"半径 {radius_m}m は分解能 {resolution_m}m に対して小さすぎます")
        size = 2 * r + 1
        yy, xx = np.ogrid[-r : r + 1, -r : r + 1]
        disk = (xx**2 + yy**2) <= r**2

        # 各方位の光線上の画素（中心を除く 1..r ステップ）
        az = np.linspace(0, 2 * np.pi, n_azimuths, endpoint=False)
        steps = np.arange(1, r + 1)
        dy = np.rint(np.sin(az)[:, None] * steps[None, :]).astype(int) + r
        dx = np.rint(np.cos(az)[:, None] * steps[None, :]).astype(int) + r
        ray_index = (dy * size + dx).astype(np.int32)
        return cls(radius_px=r, disk=disk, ray_index=ray_index)

    @property
    def effective_radius_m(self) -> float:
        return float(self.radius_px)


def _gather_windows(dem: np.ndarray, rows: np.ndarray, cols: np.ndarray, r: int) -> np.ndarray:
    """各点を中心とする (2r+1)² の窓を切り出す。範囲外は NaN で埋める。"""
    size = 2 * r + 1
    padded = np.pad(dem, r, mode="constant", constant_values=np.nan)
    # padded 上では中心が (row + r, col + r) なので、窓の左上は (row, col)
    off = np.arange(size)
    ri = rows[:, None, None] + off[None, :, None]
    ci = cols[:, None, None] + off[None, None, :]
    return padded[ri, ci]


def compute_delta_h(
    dem_path,
    points_xy: np.ndarray,
    radii_m: list[float],
    epsg: int | None = None,
    n_azimuths: int = 32,
    batch: int = 2000,
) -> dict[str, np.ndarray]:
    """サンプル点について標高・Δh・閉じ込み深さを算出する。

    戻り値のキー: elevation, delta_h_{r}, enclosure_{r}
    """
    from kansui.crs import zone_of_epsg

    with rasterio.open(dem_path) as src:
        got = src.crs.to_epsg()
        try:
            zone_of_epsg(got)   # ★平面直角座標系（EPSG:6669〜6687）でなければ弾く
        except ValueError:
            raise ValueError(
                f"標高ラスタの CRS が平面直角座標系ではない: {src.crs}。"
                "距離をメートルで扱えないため続行できない（T-05）。"
            ) from None
        if epsg is not None and got != epsg:
            raise ValueError(f"標高ラスタの CRS が EPSG:{epsg} ではない: {src.crs}")
        dem = src.read(1).astype(np.float32)
        res = float(src.res[0])
        inv = ~src.transform

    n = len(points_xy)
    cols_f, rows_f = inv * (points_xy[:, 0], points_xy[:, 1])
    rows = np.rint(rows_f).astype(np.int64)
    cols = np.rint(cols_f).astype(np.int64)

    inside = (rows >= 0) & (rows < dem.shape[0]) & (cols >= 0) & (cols < dem.shape[1])
    out: dict[str, np.ndarray] = {"elevation": np.full(n, np.nan, dtype=np.float32)}
    out["elevation"][inside] = dem[rows[inside], cols[inside]]

    for radius_m in radii_m:
        nb = Neighborhood.build(radius_m, res, n_azimuths)
        key = f"{int(radius_m)}"
        dh = np.full(n, np.nan, dtype=np.float32)
        enc = np.full(n, np.nan, dtype=np.float32)
        disk_flat = nb.disk.ravel()
        log.info("Δh(r=%dm): 半径 %dpx（実効 %.1fm） / 円形近傍 %d px / 方位 %d",
                 radius_m, nb.radius_px, nb.radius_px * res, int(nb.disk.sum()), n_azimuths)

        idx = np.where(inside)[0]
        for s in range(0, len(idx), batch):
            sel = idx[s : s + batch]
            win = _gather_windows(dem, rows[sel], cols[sel], nb.radius_px)
            flat = win.reshape(len(sel), -1)
            centre = flat[:, flat.shape[1] // 2]

            masked = np.where(disk_flat[None, :], flat, np.nan)
            disk_valid = np.isfinite(masked).any(axis=1)
            with np.errstate(invalid="ignore"), warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                dmin = np.nanmin(masked, axis=1)
                # 方位ごとの縁の高さ = 光線上の最高標高
                rays = flat[:, nb.ray_index]                       # (batch, n_az, r)
                rim = np.nanmax(rays, axis=2)                      # (batch, n_az)

            dh_batch = np.where(disk_valid, centre - dmin, np.nan)
            dh[sel] = dh_batch

            # ★全方位に有効な縁があるときだけ「閉じ込み」を確定させる。
            #   ある方位の光線が丸ごと欠損（＝海や無データ域）だと、その方位を無視して
            #   nanmin を取ってしまい、**海に排水できる沿岸道路を「囲まれている」と誤判定する。**
            #   欠損方位があれば NaN のままにし、下流で「不明」として明示的に扱う
            #   （T-09 と同じ方針：推定値で埋めない）。
            ray_known = np.isfinite(rays).any(axis=2)              # (batch, n_az)
            all_known = ray_known.all(axis=1)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                enc_batch = np.nanmin(rim, axis=1) - centre
            enc[sel] = np.where(all_known, enc_batch, np.nan)

        out[f"delta_h_{key}"] = dh
        out[f"enclosure_{key}"] = enc
    return out
