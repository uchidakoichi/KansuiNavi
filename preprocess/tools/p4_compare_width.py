"""P-4 その2: 国土地理院の幅員区分 vs PLATEAU 実測幅員（藤沢市）。

★問い: 全国で取れる `vt_rnkwidth`（幅員区分）は、PLATEAU の実測幅員の代わりになるか。

正解データ: 藤沢版が PLATEAU の道路面（LOD1）から実測した幅員（市域エッジの 94.0%）。
  ★測っているのは**道路区域の幅（歩道・路肩を含む）**であって車道の幅ではない。
  ★地理院の区分が何の幅かは**仕様書に書かれていない**（2026-09-20 に全文を確認）。
    → 「ずれ」なのか「測っているものが違う」のかは、この比較だけでは決められない。

照合の方法（★保守的に組む）:
  エッジの中点から最近傍の RdCL 線分を探し、**向きが 20 度以上違うものは捨てる**。
  交差する別の道に当たった対応を混ぜないため。拾えなかったぶんは率で報告する。
"""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

from kansui.crs import Place, epsg_for
from kansui.gsi_vector import RNKWIDTH_RANGE

FUJISAWA = Path(__file__).resolve().parents[3] / "Kansui/preprocess/data/raw"  # 藤沢版のリポジトリを隣に置いた前提
RDCL = Path("data/raw/gsi_rdcl_fujisawa.parquet")

MAX_SNAP_M = 12.0      # 中点から RdCL までの許容距離
MAX_BEARING_DEG = 20.0  # 向きの許容差（0〜90度で評価。180度の周期を畳む）
MIN_EDGE_M = 30.0      # ★短いエッジは交差点付近で曖昧なので使わない

#: 区分の並び（狭い→広い）。単調性の確認に使う
ORDER = ["3m未満", "3m-5.5m未満", "5.5m-13m未満", "13m-19.5m未満", "19.5m以上"]


def _bearing(x0, y0, x1, y1):
    """0〜180度の向き（線分に向きの区別は無いので周期 180 に畳む）。"""
    return np.degrees(np.arctan2(y1 - y0, x1 - x0)) % 180.0


def _bearing_diff(a, b):
    d = np.abs(a - b) % 180.0
    return np.minimum(d, 180.0 - d)


def main() -> None:
    epsg = epsg_for(Place(pref_code="14", municipality="藤沢市"))
    print(f"藤沢市の平面直角座標系: EPSG:{epsg}（第IX系）\n")

    # --- 地理院 RdCL -----------------------------------------------------------
    gsi = gpd.read_parquet(RDCL).to_crs(epsg)
    print(f"RdCL: {len(gsi):,} 本")
    rnk = gsi["vt_rnkwidth"].value_counts(dropna=False)
    print("\n★vt_rnkwidth の充足（藤沢市域・z16）")
    for k, v in rnk.items():
        print(f"    {str(k):<16} {v:>7,}  {v / len(gsi):>6.1%}")
    usable = gsi["vt_rnkwidth"].isin(ORDER).mean()
    print(f"    → 区分が付いている割合: {usable:.2%}")

    # --- 藤沢版のエッジ＋実測幅員 -----------------------------------------------
    edges = gpd.read_file(FUJISAWA / "roads" / "road_edges.gpkg", layer="edges").to_crs(epsg)
    width = pd.read_parquet(FUJISAWA / "road_width.parquet")
    # ★行の並びで突き合わせている。u/v が一致することを確かめてから使う
    #   （ずれていると、まったく別の道の幅員を比較することになる）
    assert len(width) == len(edges), (len(width), len(edges))
    assert (width["u"].to_numpy() == edges["u"].to_numpy()).all()
    assert (width["v"].to_numpy() == edges["v"].to_numpy()).all()
    edges["w_median"] = width["w_median"].to_numpy()
    boundary = gpd.read_file(FUJISAWA / "fujisawa_boundary.geojson").to_crs(epsg).union_all()

    sel = (
        edges["w_median"].notna()
        & (edges.length >= MIN_EDGE_M)
        & edges.geometry.within(boundary)
    )
    e = edges.loc[sel].copy()
    print(f"\n照合の対象エッジ: {len(e):,} 本"
          f"（実測幅員あり・{MIN_EDGE_M:.0f}m 以上・市域内）")

    # 中点と向き
    coords = [np.asarray(g.coords) for g in e.geometry]
    mid = np.array([np.asarray(g.interpolate(0.5, normalized=True).coords)[0] for g in e.geometry])
    e_bear = np.array([_bearing(c[0, 0], c[0, 1], c[-1, 0], c[-1, 1]) for c in coords])
    pts = gpd.GeoDataFrame(
        {"w_median": e["w_median"].to_numpy(), "highway": e["highway"].to_numpy(),
         "bear": e_bear},
        geometry=[Point(p) for p in mid], crs=epsg,
    )

    # --- RdCL を2点の線分に割る -------------------------------------------------
    segs, props = [], []
    from shapely.geometry import LineString
    for geom, w in zip(gsi.geometry, gsi["vt_rnkwidth"], strict=True):
        c = np.asarray(geom.coords)
        for i in range(len(c) - 1):
            segs.append(LineString([c[i], c[i + 1]]))
            props.append((w, _bearing(c[i, 0], c[i, 1], c[i + 1, 0], c[i + 1, 1])))
    sg = gpd.GeoDataFrame(
        {"rnk": [p[0] for p in props], "gbear": [p[1] for p in props]},
        geometry=segs, crs=epsg,
    )
    print(f"RdCL の線分: {len(sg):,}")

    # --- 最近傍 ＋ 向きでふるい ---------------------------------------------------
    j = gpd.sjoin_nearest(pts, sg, max_distance=MAX_SNAP_M, distance_col="dist", how="left")
    j = j[~j.index.duplicated(keep="first")]  # 同距離の重複を1つに
    matched = j["rnk"].notna()
    ok = matched & (_bearing_diff(j["bear"].to_numpy(), j["gbear"].to_numpy()) <= MAX_BEARING_DEG)
    print(f"\n照合できた: {matched.sum():,} / {len(j):,} ({matched.mean():.1%})"
          f" → 向きが合うもの {ok.sum():,} ({ok.mean():.1%})")
    m = j.loc[ok & j["rnk"].isin(ORDER)].copy()
    print(f"★比較に使う対応: {len(m):,} 本  （スナップ距離 中央値 {m['dist'].median():.1f}m"
          f" / p90 {m['dist'].quantile(0.9):.1f}m）")

    # --- 本題: 区分ごとの実測値 ---------------------------------------------------
    print(f"\n{'='*78}\n★区分ごとの PLATEAU 実測幅員［m］\n{'='*78}")
    print(f"{'区分':<16}{'区分の範囲':>14}{'n':>8}{'中央値':>9}{'p25':>8}{'p75':>8}"
          f"{'範囲内':>8}")
    rows = []
    for k in ORDER:
        s = m.loc[m["rnk"] == k, "w_median"]
        if not len(s):
            continue
        lo, hi = RNKWIDTH_RANGE[k]
        inside = ((s >= (lo if lo is not None else -np.inf))
                  & (s < (hi if hi is not None else np.inf))).mean()
        label = f"{lo if lo is not None else ''}〜{hi if hi is not None else ''}"
        print(f"{k:<16}{label:>14}{len(s):>8,}{s.median():>9.2f}"
              f"{s.quantile(.25):>8.2f}{s.quantile(.75):>8.2f}{inside:>8.1%}")
        rows.append((k, len(s), s.median(), inside))

    meds = [r[2] for r in rows]
    mono = all(a < b for a, b in zip(meds, meds[1:], strict=False))
    print(f"\n★中央値は単調に増えるか: {'はい' if mono else 'いいえ'}  "
          f"（{' < '.join(f'{v:.2f}' for v in meds)}）")
    overall_inside = np.mean([
        (m["w_median"].to_numpy()[i] >= (RNKWIDTH_RANGE[k][0] or -np.inf))
        and (m["w_median"].to_numpy()[i] < (RNKWIDTH_RANGE[k][1] or np.inf))
        for i, k in enumerate(m["rnk"].to_numpy())
    ])
    print(f"★実測値が区分の範囲に入る割合（全体）: {overall_inside:.1%}")

    # 1段階ずれまで許すとどうか
    idx = {k: i for i, k in enumerate(ORDER)}

    def band_of(w):
        for i, k in enumerate(ORDER):
            lo, hi = RNKWIDTH_RANGE[k]
            if (lo is None or w >= lo) and (hi is None or w < hi):
                return i
        return len(ORDER) - 1

    actual = np.array([band_of(w) for w in m["w_median"]])
    claimed = np.array([idx[k] for k in m["rnk"]])
    d = actual - claimed
    print(f"★1段階のずれまで許すと: {(np.abs(d) <= 1).mean():.1%}")
    print("  ずれの内訳: "
          + " / ".join(f"{k:+d}段 {(d == k).mean():.1%}" for k in range(-2, 3)))
    print(f"  ★符号: 実測のほうが広い {(d > 0).mean():.1%} / 狭い {(d < 0).mean():.1%}")

    # --- OSM の道路種別との関係 ---------------------------------------------------
    print(f"\n{'='*78}\n参考: OSM 道路種別ごと（実測中央値 / 最頻の区分）\n{'='*78}")
    for hw, sub in m.groupby("highway"):
        if len(sub) < 30:
            continue
        print(f"  {str(hw):<16} n={len(sub):>6,}  実測 {sub['w_median'].median():>6.2f}m"
              f"   最頻区分 {sub['rnk'].mode().iat[0]}")

    m.drop(columns="geometry").to_csv("data/raw/p4_width_match.csv", index=False)
    print("\n→ data/raw/p4_width_match.csv")


if __name__ == "__main__":
    sys.exit(main())
