"""標高タイルの列挙・取得・被覆率。

★**Δh は前処理で計算してグラフに載せる**（案A）。
  実行時にブラウザが標高タイルを取る案Bは 2026-09-20 に実測で棄却した
  （1経路あたり 5.7〜14.6MB 要る）。`docs/StepN2_設計_地域ごとの段階導入.md` §1。

★絶対ルール2: 標高 API（`getelevation.php`）は使用禁止。タイルの一括取得で得る。
★絶対ルール3: 逐次・sleep・同時接続1・キャッシュ・UA 明示（`kansui.fetcher`）。

★**低ズームを取っても解像度は上がらない。** 最大ZL未満は上位ZLの4点平均で生成される。
  必ず種別ごとの最大ZLを叩く（`config.yaml` の `dem.sources[].zoom`）。

★T-07: DEM10B は Δh の分解能が不足する。当該区画は**リスク算出の対象外**として
  明示する（推定値で埋めない）。全国では広範囲に効きうる（NK-07）。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from kansui.dem_png import decode_dem_png
from kansui.fetcher import NOT_FOUND

if TYPE_CHECKING:  # pragma: no cover
    from shapely.geometry.base import BaseGeometry

log = logging.getLogger(__name__)

TILE_PX = 256


@dataclass(frozen=True)
class DemSource:
    """`config.yaml` の `dem.sources[]` の1行。"""

    name: str
    zoom: int
    sd_m: float
    url: str

    @classmethod
    def from_config(cls, d: dict) -> DemSource:
        return cls(name=str(d["name"]), zoom=int(d["zoom"]),
                   sd_m=float(d["sd_m"]), url=str(d["url"]))


def fallback_chain(sources: list[dict], primary: str) -> list[DemSource]:
    """主系列から下位へのフォールバック順。

    ★藤沢では DEM1A は14倍のコストに対し Δh の差が 0.15% だったので使わない。
      `primary` より**前**の種別は順番に入れない。
    """
    chain = [DemSource.from_config(d) for d in sources]
    names = [s.name for s in chain]
    if primary not in names:
        raise ValueError(f"dem.primary '{primary}' が dem.sources に無い: {names}")
    return chain[names.index(primary):]


# -----------------------------------------------------------------------------
# タイルの列挙
# -----------------------------------------------------------------------------
def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2**z
    return (int((lon + 180.0) / 360.0 * n),
            int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n))


def tile_bounds(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    """(west, south, east, north)［度］。"""
    n = 2**z
    def lat_of(yy: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * yy / n))))
    return (x / n * 360.0 - 180.0, lat_of(y + 1), (x + 1) / n * 360.0 - 180.0, lat_of(y))


def tiles_for_polygon(poly: BaseGeometry, z: int) -> list[tuple[int, int]]:
    """ポリゴンに**実際に重なる**タイル番号の一覧。

    ★bbox で列挙しない。神奈川県では bbox 6,030枚に対し実際は 3,620枚だった
      （海と隣県を 2,410枚ぶん無駄に叩くことになる）。
    """
    from shapely.geometry import box

    w, s, e, n = poly.bounds
    x0, y0 = lonlat_to_tile(w, n, z)
    x1, y1 = lonlat_to_tile(e, s, z)
    return [(x, y)
            for y in range(y0, y1 + 1)
            for x in range(x0, x1 + 1)
            if box(*tile_bounds(x, y, z)).intersects(poly)]


# -----------------------------------------------------------------------------
# 取得
# -----------------------------------------------------------------------------
@dataclass
class DemStats:
    """種別ごとの結果。★被覆率（NK-07）はここから出す。"""

    per_source: dict[str, int] = field(default_factory=dict)
    missing: int = 0            # 全種別で取れなかったタイル
    tiles: int = 0
    nodata_px: int = 0
    total_px: int = 0

    def report(self, chain: list[DemSource]) -> str:
        lines = [f"タイル {self.tiles:,} 枚"]
        for s in chain:
            k = self.per_source.get(s.name, 0)
            if k:
                lines.append(f"  {s.name:<7} {k:>6,} 枚 ({k / self.tiles:6.2%})"
                             f"  標高精度SD {s.sd_m}m")
        if self.missing:
            lines.append(f"  ★どの種別でも取れず {self.missing:,} 枚 "
                         f"({self.missing / self.tiles:.2%}) ── 標高不明として扱う")
        if self.total_px:
            lines.append(f"  画素の欠損率 {self.nodata_px / self.total_px:.4%}")
        return "\n".join(lines)


class DemFetchError(RuntimeError):
    """標高タイルが通信の失敗で取れない。★404（タイルが無い）とは区別する。"""


def fetch_tile_with_fallback(
    fetcher, chain: list[DemSource], x: int, y: int, primary_z: int
) -> tuple[str | None, np.ndarray | None]:
    """1タイルを主系列から順に試す。

    ★**種別ごとにズームが違う**ので、下位に落ちるときはタイル番号を引き直す。
      同じ x/y を別のズームで叩くと**まったく別の場所**を取ってしまう。
    ★T-09: 全画素が欠損なら「取れた」と見なさず次の種別へ落ちる。
      RGB(128,0,0) は「標高0m」ではない。
    """
    w, s, e, n = tile_bounds(x, y, primary_z)
    clon, clat = (w + e) / 2, (s + n) / 2
    for src in chain:
        if src.zoom == primary_z:
            tx, ty = x, y
        else:
            tx, ty = lonlat_to_tile(clon, clat, src.zoom)
        url = src.url.format(z=src.zoom, x=tx, y=ty)
        data = fetcher.fetch(url)
        if data is None:
            # ★★通信の失敗（やり直しても取れなかった）は「タイルが無い」ではない。
            #   ここで次の種別へ落ちると、障害のあいだの範囲が**欠損（判定不能）や
            #   精度の低い種別として焼き付き**、区画キャッシュと印に残って再実行でも直らない
            #   （2026-09-23 のレビューで指摘）。→ 止めて、その地域を作り直させる。
            raise DemFetchError(f"標高タイルを取得できない（通信の失敗）: {url}")
        if data is NOT_FOUND:
            continue
        arr = decode_dem_png(data)
        if np.isnan(arr).all():
            continue  # ★全欠損は「取れた」と見なさない
        return src.name, arr
    return None, None


def upsample_part(a: np.ndarray, x: int, y: int, dz: int) -> np.ndarray:
    """ズームが dz 小さいタイル a から、主系列のタイル (x, y) に当たる部分を切り出して拡大する。

    ★主系列のタイル (x, y) は、ズームの小さいタイルの中で
      列 x mod 2^dz・行 y mod 2^dz の位置にある（タイル番号の下位ビット）。最近傍で拡大。
    """
    f = 2 ** dz
    size = a.shape[0] // f
    oy, ox = (y % f) * size, (x % f) * size
    return np.repeat(np.repeat(a[oy:oy + size, ox:ox + size], f, axis=0), f, axis=1)


def fetch_tile_filled(
    fetcher, chain: list[DemSource], x: int, y: int, primary_z: int,
    water=None, land_hole_min_frac: float | None = None,
) -> tuple[np.ndarray | None, np.ndarray | None]:
    """主系列のタイル (x, y) の標高を、**画素単位で**下位の種別から埋めて返す。

    戻り値: (標高 float32 256×256・欠損は NaN, 主系列よりズームの小さい種別から埋めた画素の印 bool)。
            どの種別にも値が無ければ (None, None)。

    ★★2026-09-29、`fetch_tile_with_fallback`（タイル単位の切り替え）では**タイル内の穴が残る**と判明。
      DEM5A のタイルが1画素でもあればそれを採り、道路の上の欠損画素は DEM5B・5C・10B で埋まらなかった
      （全国で判定不能のまま残った道路 16,913km のうち、標本の 93〜98% がこれ）。
      → 穴（NaN）の画素だけを次の種別で埋める。値のある画素は上位の種別のまま（精度の高い順）。
    ★ズームの違う種別は切り出して拡大してから使う（→ upsample_part）。
    ★通信の失敗は止める（→ DemFetchError。欠損として焼き付けない）。
    ★★**ズームの小さい種別（DEM10B）は、DEM5A〜5C のどれにも値が1画素も無いタイルでだけ使う。**
      DEM5A は川・池などの水面を欠損にしている。画素単位で DEM10B を入れると水面まで埋まり、
      DEM5A が全域にある関東でも川沿いの道路の 3〜5 割に「10mメッシュで推定」の印が付き、
      Δh も水面の標高で変わってしまった（2026-09-29・7回目のレビューで指摘）。
      NK-07 の趣旨は「DEM5 が**整備されていない**所を埋める」であって、水面を埋めることではない。
    ★★2026-09-30: 水域のマスク `water(x, y)`（→ kansui.water）を渡すと、DEM5 が一部あるタイルでも、
      **水域の外の欠損がタイルの `land_hole_min_frac` 以上**あれば、その画素（水域の外だけ）を DEM10B で埋める。
      DEM5 が1画素も無いタイルでも、水域の画素は埋めない。
      実測（各地域800枚）: 水域の外の欠損がタイルの 10% を超えるのは 関東 0.6% / 秋田 28.7% / 道北 58.9%。
      関東の川原の欠損は多くてもタイルの 3% ほどで、未整備の陸地（数十%〜全域）とはっきり分かれる。
      `water` を渡さなければ従来どおり（DEM5 が1画素も無いタイルだけ、水面も含めて埋める）。
    """
    w, s, e, n = tile_bounds(x, y, primary_z)
    clon, clat = (w + e) / 2, (s + n) / 2
    arr: np.ndarray | None = None
    low = np.zeros((256, 256), dtype=bool)
    for src in chain:
        if src.zoom == primary_z:
            tx, ty = x, y
        elif src.zoom < primary_z:
            tx, ty = lonlat_to_tile(clon, clat, src.zoom)
        else:
            raise ValueError(f"主系列より細かい種別 {src.name}（z{src.zoom}）は扱えない")
        fillable = None   # ズームの小さい種別で埋めてよい画素（None = 欠損すべて）
        if src.zoom < primary_z:
            wm = water(x, y) if water is not None else None
            if arr is not None and np.isfinite(arr).any():
                if wm is None:
                    break   # ★DEM5 が一部でもある＝整備されている。残る欠損は水面等なので埋めない
                land_hole = np.isnan(arr) & ~wm
                if land_hole.mean() < land_hole_min_frac:
                    break   # ★水域の外の欠損が小さい＝川原・護岸の縁など。埋めない
                fillable = land_hole
            elif wm is not None:
                fillable = ~wm
        url = src.url.format(z=src.zoom, x=tx, y=ty)
        data = fetcher.fetch(url)
        if data is None:
            raise DemFetchError(f"標高タイルを取得できない（通信の失敗）: {url}")
        if data is NOT_FOUND:
            continue
        a = decode_dem_png(data)
        if src.zoom < primary_z:
            a = upsample_part(a, x, y, primary_z - src.zoom)
        if fillable is not None:
            a = np.where(fillable, a, np.nan).astype(np.float32)
        if arr is None:
            arr = a.astype(np.float32, copy=True)
            if src.zoom < primary_z:
                low |= np.isfinite(arr)
        else:
            hole = np.isnan(arr) & np.isfinite(a)
            arr[hole] = a[hole]
            if src.zoom < primary_z:
                low |= hole
        if not np.isnan(arr).any():
            break
    if arr is None or np.isnan(arr).all():
        return None, None
    return arr, low


def fetch_region(
    fetcher, chain: list[DemSource], poly: BaseGeometry, on_tile=None
) -> DemStats:
    """地域ぶんの標高タイルを取る。★逐次。並列にしない。"""
    primary = chain[0]
    targets = tiles_for_polygon(poly, primary.zoom)
    st = DemStats(tiles=len(targets))
    log.info("標高タイル %s z%d: %d 枚", primary.name, primary.zoom, len(targets))

    for i, (x, y) in enumerate(targets, 1):
        name, arr = fetch_tile_with_fallback(fetcher, chain, x, y, primary.zoom)
        if name is None:
            st.missing += 1
        else:
            st.per_source[name] = st.per_source.get(name, 0) + 1
            st.nodata_px += int(np.isnan(arr).sum())
            st.total_px += arr.size
        if on_tile:
            on_tile(i, len(targets), x, y, name)
    return st
