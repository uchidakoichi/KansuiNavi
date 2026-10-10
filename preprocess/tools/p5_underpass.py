"""P-5: アンダーパスの幾何推定を地域全体に適用する。

★問い（設計 §5 P-5）: 適合率 96.7% は藤沢での値。他地域でも当たるか。

★**この スクリプトは適合率を測らない。** 測るのは
  - 検出数と、道路延長あたりの**出現率**（藤沢と比べられる形）
  - タグ（tunnel / layer 負）と**幾何推定のどちらで拾えたか**の内訳
    ＝ T-01 の取りこぼし量
  - 上を通るものの種別（鉄道 / 道路 / 歩行者用 = 除外）

  適合率は**無作為抽出した候補を目視で照合**して初めて出る（藤沢版 V-2 と同じやり方）。
  ★単一判定者の目視は再現しないことが藤沢版で実測済みなので、
    **水準（dip/flat）の判定はしない。** 「これはアンダーパスか」だけを測る。

★`exclude`（歩行者用の橋の下）を落とすこと。藤沢市域では交差地点216のうち
  93地点（43%）が歩道橋の下だった。落とさないと大幅に過大警告になる。
"""

from __future__ import annotations

import logging
import sys
import time
from collections import Counter

import geopandas as gpd
import pandas as pd

from kansui import boundary as B
from kansui import underpass as U
from kansui.config import Config
from kansui.fetcher import TileFetcher
from kansui.region import Region, region_epsg

ROADS = "data/raw/roads"

#: 藤沢版の実測（比較用）。★**同じ基準（市域+3km・クラスタ後）に揃えてある。**
#
#   ★2026-09-20 に一度取り違えた。CLAUDE.md にある「112」は**市域内限定**の数字で、
#     道路延長 4,451km は**市域+3km**のもの。混ぜて割ると 2.52/100km という
#     誤った出現率になり、「神奈川は藤沢の4.1倍」という誤った結論が出た。
#     正しくは `underpass_candidates.gpkg` から数え直して **480 地点**。
#   → **分子と分母の範囲を必ず揃えること。**
FUJISAWA = {
    "edges": 177_542,      # 市域+3km のエッジ
    "km": 4_451,           # 同上の道路延長
    "points": 1_461,       # 除外後の交差地点（underpass_candidates.gpkg）
    "clusters": 480,       # ★30m でまとめた地点数
    "unknown_edges": 880,  # risk.state == "underpass" のエッジ
    "tagged_share": 0.0253,
    "precision": 0.967,    # V-2 の目視照合（120地点）
}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = Config.load()
    region = Region.from_env(cfg)
    gdf = B.load(TileFetcher.from_config(cfg), cfg.require("boundary.url_template"),
                 region, cfg.resolve_path("boundary.cache_dir"))
    epsg = region_epsg(gdf)
    print(f"地域 {region.name} / EPSG:{epsg}", flush=True)

    edges = gpd.read_parquet(f"{ROADS}/edges_{region.name}.parquet").to_crs(epsg)
    bridges = gpd.read_parquet(f"{ROADS}/bridges_{region.name}.parquet").to_crs(epsg)
    km = edges.length.sum() / 1000
    print(f"エッジ {len(edges):,} 本 / {km:,.0f} km / bridge way {len(bridges):,} 本\n",
          flush=True)

    t0 = time.time()
    seps = U.find_grade_separations(edges, bridges, epsg)
    print(f"交差地点（頂点を共有しない） {len(seps):,} 件（{time.time() - t0:.0f}秒）",
          flush=True)
    if seps.empty:
        print("★0件。bbox か入力を確認すること")
        return 1

    seps = U.merge_with_tags(seps, edges)
    seps["upper"] = [U.classify_upper(bridges, i) for i in seps["bridge_idx"]]
    print(f"\n上を通るものの種別: {dict(Counter(seps['upper']))}", flush=True)
    ex = (seps["upper"] == "exclude").mean()
    print(f"  ★歩行者用の橋の下（除外） {ex:.1%}"
          "  （★藤沢は市域内限定の 43.1% しか記録が無く、同じ基準で比べられない）",
          flush=True)

    keep = seps[seps["upper"] != "exclude"].copy()
    print(f"  → 残る地点 {len(keep):,} 件", flush=True)

    # --- ★タグで拾えたか、幾何推定でしか拾えなかったか --------------------------
    tagged = keep["tagged"]
    print(f"\n{'=' * 68}\n★T-01 の取りこぼし量\n{'=' * 68}", flush=True)
    print(f"  タグ（tunnel / layer 負）でも拾える  {tagged.sum():>7,} ({tagged.mean():6.2%})",
          flush=True)
    print(f"  ★**幾何推定でしか拾えない**        {(~tagged).sum():>7,} "
          f"({(~tagged).mean():6.2%})"
          f"  （藤沢市+3km は {1 - FUJISAWA['tagged_share']:.2%}）", flush=True)
    print("  → 幾何推定を実装しないと、この分を丸ごと取りこぼす", flush=True)

    # --- 地点へのまとめ ---------------------------------------------------------
    cl = U.cluster_points(keep, radius_m=30.0)
    by_cluster = cl.groupby("cluster")
    rows = []
    for cid, sub in by_cluster:
        rows.append({
            "cluster": int(cid),
            "n_points": len(sub),
            "upper": U.dominant_upper(sub["upper"]),
            "tagged": bool(sub["tagged"].any()),
            "highway": sub["highway"].mode().iat[0] if len(sub["highway"].mode()) else None,
            "name": sub["name"].dropna().iat[0] if sub["name"].notna().any() else None,
            "x": sub["x"].mean(), "y": sub["y"].mean(),
        })
    cdf = pd.DataFrame(rows)
    print(f"\n{'=' * 68}\n★地点にまとめた結果（30m でクラスタ）\n{'=' * 68}", flush=True)
    print(f"  地点数 {len(cdf):,}", flush=True)
    print(f"  根拠の内訳: {dict(Counter(cdf['upper']))}", flush=True)
    print(f"  タグでも拾える地点 {cdf['tagged'].mean():.1%}", flush=True)

    n_edges = keep["edge_idx"].nunique()
    print(f"\n★判定不能になるエッジ: {n_edges:,} / {len(edges):,} "
          f"({n_edges / len(edges):.2%})", flush=True)

    print("\n★出現率（★分子と分母の範囲を揃えてある）", flush=True)
    print(f"  {'':<14}{'地点数':>8}{'道路km':>10}{'100kmあたり':>12}"
          f"{'判定不能エッジ':>14}", flush=True)
    print(f"  {region.name:<14}{len(cdf):>8,}{km:>10,.0f}"
          f"{len(cdf) / km * 100:>12.2f}{n_edges / len(edges):>14.2%}", flush=True)
    print(f"  {'藤沢市+3km':<14}{FUJISAWA['clusters']:>8,}{FUJISAWA['km']:>10,}"
          f"{FUJISAWA['clusters'] / FUJISAWA['km'] * 100:>12.2f}"
          f"{FUJISAWA['unknown_edges'] / FUJISAWA['edges']:>14.2%}", flush=True)

    print("\n  道路種別ごとの地点数（上位）:", flush=True)
    for hw, n in Counter(cdf["highway"].dropna()).most_common(8):
        print(f"    {str(hw):<16} {n:>6,}", flush=True)

    out = gpd.GeoDataFrame(
        cdf, geometry=gpd.points_from_xy(cdf["x"], cdf["y"]), crs=epsg
    ).to_crs(4326)
    path = f"{ROADS}/underpass_{region.name}.geojson"
    out.to_file(path, driver="GeoJSON")
    print(f"\n→ {path}", flush=True)
    print("\n★適合率はここでは測っていない。無作為抽出した候補の目視照合が要る"
          "（藤沢版 V-2 と同じやり方）。★水準（dip/flat）の判定はしない。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
