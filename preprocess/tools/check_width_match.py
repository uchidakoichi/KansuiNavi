"""幅員区分（`vt_rnkwidth`）の照合率を、**落ちた側の内訳まで**出す。★NK-18 の証拠。

★なぜ道具にするか（2026-09-21）:
  「照合できた 74.83%」という1つの数字だけでは、**幅員の問題なのか別の問題なのか
  分からなかった。** 内訳を取って初めて原因が見えた:

      県内 94.0%  /  県外（+3km の外）31.9%

  → 幅員の照合の問題ではなく、**道路グラフが設計（+3km）より広かった**のが原因。
    道路は bbox で取り出して切り直していなかったのに対し、
    幅員タイルは**地域の形**で取っていたため、そこで食い違いが表に出た。

★率だけを見て「まあまあ取れている」と流さないこと。**落ちた側を必ず割って見る。**

使い方:
  uv run python tools/check_width_match.py
"""

from __future__ import annotations

import logging
import sys

import geopandas as gpd
import numpy as np

from kansui import boundary as B
from kansui import roads as R
from kansui import width as W
from kansui.config import Config
from kansui.fetcher import TileFetcher
from kansui.region import Region, region_epsg

ROADS = "data/raw/roads"
GSI = "data/raw/gsi"


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    cfg = Config.load()
    region = Region.from_env(cfg)
    gdf = B.load(TileFetcher.from_config(cfg), cfg.require("boundary.url_template"),
                 region, cfg.resolve_path("boundary.cache_dir"))
    epsg = region_epsg(gdf)

    e = gpd.read_parquet(f"{ROADS}/edges_{region.name}.parquet").to_crs(epsg)
    gsi = gpd.read_parquet(f"{GSI}/rdcl_{region.name}.parquet").to_crs(epsg)
    print(f"地域 {region.name} / EPSG:{epsg}")
    print(f"エッジ {len(e):,} / RdCL {len(gsi):,} 本\n")

    rnk, st = W.assign(e, gsi, epsg, progress=lambda m: print(f"  {m}", flush=True))
    e = e.copy()
    e["matched"] = [r is not None for r in rnk]
    e["len_m"] = e.length

    print(f"\n★全体の照合率: {st['matched']:,} / {st['edges']:,} ({st['matched_share']:.2%})")
    print(f"  スナップ距離 中央値 {st['snap_median_m']:.2f}m / p90 {st['snap_p90_m']:.2f}m")

    print("\n★区分ごと")
    for k in W.RNKWIDTH_ORDER:
        c = st["by_class"].get(k, 0)
        print(f"  {k:<16}{c:>9,}  {c / max(1, st['matched']):>7.2%}"
              f"  → 狭さ {W.narrowness_table()[k]:.3f}")

    # ★★ここが本題: 落ちた側を割って見る
    poly = gdf.to_crs(epsg).union_all()
    buf = (gpd.GeoSeries([gdf.to_crs(4326).union_all()], crs=4326)
           .pipe(lambda s: gpd.GeoSeries([R.buffered_poly(s.iloc[0], region.buffer_km)], crs=4326))
           .to_crs(epsg).iloc[0])
    inside = e.geometry.intersects(poly)
    in_buf = e.geometry.intersects(buf)
    print("\n★位置で割る（★ここで原因が見える）")
    print(f"  県内            {inside.sum():>9,}  照合 {e.loc[inside, 'matched'].mean():>7.2%}")
    print(f"  県外だが+{region.buffer_km:g}km内 "
          f"{(in_buf & ~inside).sum():>9,}  照合 "
          f"{e.loc[in_buf & ~inside, 'matched'].mean():>7.2%}")
    n_out = int((~in_buf).sum())
    print(f"  ★+{region.buffer_km:g}km の外    {n_out:>9,}  照合 "
          f"{e.loc[~in_buf, 'matched'].mean() if n_out else float('nan'):>7.2%}"
          + ("   ← ★設計より広い。切り直すこと" if n_out else "   ← 0 でよい"))

    print("\n★道路種別ごと（上位）")
    t = (e.groupby("highway")
         .agg(n=("matched", "size"), r=("matched", "mean"), L=("len_m", "median"))
         .sort_values("n", ascending=False))
    for k, row in t.head(12).iterrows():
        print(f"  {str(k):<20}{row['n']:>9,.0f}  {row['r']:>7.1%}  中央長 {row['L']:>6.1f}m")

    print("\n★長さごと")
    for lo, hi in [(0, 10), (10, 20), (20, 30), (30, 50), (50, np.inf)]:
        m = (e["len_m"] >= lo) & (e["len_m"] < hi)
        if m.sum():
            print(f"  {lo:>3}–{'' if hi == np.inf else int(hi):<4}m  {m.sum():>9,}"
                  f"  照合 {e.loc[m, 'matched'].mean():>7.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
