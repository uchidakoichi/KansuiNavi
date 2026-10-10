"""P-2 その1: 地域の道路網を取り出し、メッシュ区画ごとの大きさを測る。

★問い（設計 §5 P-2）: どの単位で切れば配信の上限に収まり、境界をまたぐ経路が引けるか。
  測るもの:
    - 区画あたりのエッジ数・推定バイト数の**分布**（最大が 25 MiB に収まるか）
    - 地域全体のファイル数（20,000 の上限に対して）
    - ★**区画の境界をまたぐエッジの数**（＝つなぎ目の量）

★エッジの区画への割り当ては**両端ノードの区画**で見る。中点で決めると、
  またぐエッジが「片方の区画にしか無い」状態になって経路が切れる。
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from collections import Counter

import geopandas as gpd
import numpy as np

from kansui import boundary as B
from kansui import roads as R
from kansui.config import Config
from kansui.mesh import mesh_code
from kansui.region import Region, region_epsg

#: 藤沢版の実測。`graph.bin` 4.63MB / エッジ 177,542本
BYTES_PER_EDGE = 26.1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = Config.load()
    region = Region.from_env(cfg)
    from kansui.fetcher import TileFetcher

    gdf = B.load(TileFetcher.from_config(cfg), cfg.require("boundary.url_template"),
                 region, cfg.resolve_path("boundary.cache_dir"))
    poly = gdf.union_all()
    epsg = region_epsg(gdf)
    bbox = R.buffered_bbox(poly, region.buffer_km)
    pbf = R.pbf_path_for_region(cfg, region.name)
    print(f"地域 {region.name}（+{region.buffer_km:g}km） / EPSG:{epsg}", flush=True)
    print(f"PBF: {pbf}", flush=True)

    t0 = time.time()
    nodes, edges = R.extract_drivable(pbf, bbox, cfg)
    print(f"\n抽出 {time.time() - t0:.0f}秒: ノード {len(nodes):,} / エッジ {len(edges):,}",
          flush=True)

    # ★★**bbox で取り出したものを、地域の形に切り直す。**
    #   bbox は矩形なので、神奈川県＋3km では東京・山梨・静岡の奥まで含む。
    #   2026-09-21 まで切り直していなかった: エッジの 21.2%（307,282本・8,227km）が
    #   設計の 3km の外にあった。★黙って動くので気づけない。
    #   ★`within` ではなく `intersects` で残す。境界をまたぐエッジを落とすと
    #     **そこで経路が切れる。**
    keep = R.buffered_poly(poly, region.buffer_km)
    n0 = len(edges)
    edges = edges[edges.geometry.intersects(keep)].reset_index(drop=True)
    print(f"★地域＋{region.buffer_km:g}km に切り直す: {n0:,} → {len(edges):,} 本"
          f"（{1 - len(edges) / n0:.1%} 減）", flush=True)

    edges = R.assign_speeds(edges, cfg)
    print(f"速度の出所: {dict(Counter(edges['speed_source']))}", flush=True)
    for col in ("layer", "bridge", "tunnel"):
        if col in edges.columns:
            n = edges[col].notna().sum()
            print(f"  {col:<8} {n:>8,} ({n / len(edges):6.2%})", flush=True)

    total_km = edges.to_crs(epsg).length.sum() / 1000
    print(f"総延長 {total_km:,.0f} km", flush=True)

    # --- 区画への割り当て（★両端ノードで見る） ---------------------------------
    g4326 = edges.to_crs(4326)
    coords = [np.asarray(g.coords) for g in g4326.geometry]
    level = int(cfg.require("split.level"))
    m_from = [mesh_code(c[0][0], c[0][1], level) for c in coords]
    m_to = [mesh_code(c[-1][0], c[-1][1], level) for c in coords]

    same = np.array([a == b for a, b in zip(m_from, m_to, strict=True)])
    print(f"\n★区画の境界をまたぐエッジ: {(~same).sum():,} / {len(edges):,} "
          f"({(~same).mean():.2%})", flush=True)

    cnt = Counter()
    for a, b in zip(m_from, m_to, strict=True):
        cnt[a] += 1
        if a != b:
            cnt[b] += 1          # ★またぐエッジは両方の区画に入れる（経路を切らない）

    n = np.array(sorted(cnt.values()))
    mb = n * BYTES_PER_EDGE / 1e6
    limit_mb = float(cfg.require("split.max_file_bytes")) / 1e6
    print(f"\n{'=' * 70}\n★区画ごとの大きさ（2次メッシュ・{len(cnt)} 区画）\n{'=' * 70}",
          flush=True)
    print(f"  エッジ数  中央値 {np.median(n):>8,.0f} / p90 {np.percentile(n, 90):>8,.0f}"
          f" / 最大 {n.max():>8,.0f}", flush=True)
    print(f"  推定バイト 中央値 {np.median(mb):>7.2f}MB / p90 {np.percentile(mb, 90):>7.2f}MB"
          f" / ★最大 {mb.max():>7.2f}MB", flush=True)
    print(f"  → 1ファイル上限 {limit_mb:.1f}MB に"
          f"{'★収まる' if mb.max() < limit_mb else '★収まらない'}", flush=True)
    print(f"  → ファイル数 {len(cnt)}（上限 {cfg.require('split.max_files'):,}）", flush=True)

    out = cfg.resolve_path("boundary.cache_dir").parent / "roads"
    out.mkdir(parents=True, exist_ok=True)
    edges["mesh_from"], edges["mesh_to"] = m_from, m_to
    # ★書き終えてから置き換える。連鎖では、この地域の抽出と前の地域の書き出しが並行で走り、
    #   書き出しがこのファイルを読むことがある（6回目のレビューで指摘）。途中の中身を読ませない
    _atomic_parquet(edges, out / f"edges_{region.name}.parquet")
    _atomic_parquet(gpd.GeoDataFrame(nodes), out / f"nodes_{region.name}.parquet")
    print(f"\n→ {out}/edges_{region.name}.parquet", flush=True)

    t0 = time.time()
    bridges = R.extract_bridges(pbf, bbox)
    print(f"bridge=* の way: {len(bridges):,} 本（{time.time() - t0:.0f}秒）", flush=True)
    _atomic_parquet(bridges, out / f"bridges_{region.name}.parquet")
    return 0


def _atomic_parquet(df, dst: Path) -> None:
    tmp = dst.with_suffix(".parquet.part")
    df.to_parquet(tmp)
    tmp.replace(dst)


if __name__ == "__main__":
    sys.exit(main())
