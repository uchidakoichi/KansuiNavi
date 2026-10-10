"""道路の「狭さ」を国土地理院の幅員区分（`vt_rnkwidth`）から決める。★NK-18。

★★**決定（2026-09-20）**: 経路計算の道幅は**地理院の幅員区分に統一する**。
  PLATEAU の実測と混ぜない。市境をまたいだ瞬間に `μ·narrow` の効き方が変わるため。

★**これは「幅の実測値」ではなく「選好の順序」である。**
  藤沢版 CLAUDE.md の「数値は選好であって幅の実測値ではない」をそのまま引き継ぐ。
  P-4（藤沢・2026-09-20）の結論も「**順序としては採用。絶対値の閾値には使わない**」。

━━ P-4 で測ったこと（藤沢市・PLATEAU の実測幅員 13,282本と照合）━━━━━━━
  無作為な2本の大小一致   地理院 **93.7%**  vs  OSM 道路種別 85.9%
  Spearman ρ              地理院 **0.741**  vs  OSM 道路種別 0.694
  区分ごとの実測中央値    3.94 < 5.51 < 11.97 < 17.95 < 25.09（★単調に増える）
  ★絶対値は合わない（区分の範囲に入るのは 51.6%）。
    ずれは**片側に偏る**（実測のほうが広い 47.2% / 狭い 1.2% ＝ 39:1）。
    PLATEAU は**道路区域の幅（歩道・路肩込み）**なので、地理院の区分は
    それより狭い何かを指していると考えると辻褄が合うが、
    ★**仕様書に定義が無いので断定しない。**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

★**最適化ベクトルタイルは試験公開。** 国土地理院が「URL やデータ構成、属性の有無や
  名称等が変わる可能性があります」と明記している。令和4年11月25日には
  **`vt_rnkwidth` の属性名・属性値の誤りが修正された実績**もある。
  → 区分の綴りが変わったら `RNKWIDTH_ORDER` に無い値になり、そのエッジは
    **NaN（＝道路種別の近似へ退避）**になる。**黙って 0 で埋めない。**

照合の方法（★P-4 と同じ。保守的に組む）:
  エッジの中点から最近傍の RdCL 線分を探し、**向きが 20 度以上違うものは捨てる**。
  交差する別の道に当たった対応を混ぜないため。拾えなかったぶんは率で報告する。
"""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger(__name__)

#: 区分の並び（狭い→広い）。★`kansui.gsi_vector.RNKWIDTH_RANGE` と同じ綴り
RNKWIDTH_ORDER = [
    "3m未満",
    "3m-5.5m未満",
    "5.5m-13m未満",
    "13m-19.5m未満",
    "19.5m以上",
]

#: 区分ごとの実測幅員の中央値［m］（P-4・藤沢市・PLATEAU 実測 13,282本）。
#: ★**全国の値ではない。** 1つの市で測った値を、区分の**間隔**を決めるのに使っている。
#:   順序は全国共通（地理院の区分そのもの）だが、間隔は藤沢由来である。
#:   ★他地域で PLATEAU が使えるようになったら測り直すこと。
MEASURED_MEDIAN_M = {
    "3m未満": 3.94,
    "3m-5.5m未満": 5.51,
    "5.5m-13m未満": 11.97,
    "13m-19.5m未満": 17.95,
    "19.5m以上": 25.09,
}

#: 照合の許容値。★P-4 と同じ値にしてある（変えるなら P-4 もやり直すこと）
MAX_SNAP_M = 12.0
MAX_BEARING_DEG = 20.0


def narrowness_table() -> dict[str, float]:
    """区分 → 狭さ（0〜1。1 が最も狭い）。

    `narrow = 1 − 中央値 / 最大の中央値`。
    ★最も広い区分がちょうど 0 になる。`μ·narrow` は「広い道を優先する」項なので、
      最も広い区分に罰を与えない形が素直。
    """
    w_max = max(MEASURED_MEDIAN_M.values())
    return {k: 1.0 - v / w_max for k, v in MEASURED_MEDIAN_M.items()}


def bearing(x0, y0, x1, y1):
    """0〜180度の向き（線分に向きの区別は無いので周期 180 に畳む）。"""
    return np.degrees(np.arctan2(y1 - y0, x1 - x0)) % 180.0


def bearing_diff(a, b):
    d = np.abs(a - b) % 180.0
    return np.minimum(d, 180.0 - d)


def _segments(gsi, epsg: int):
    """RdCL を2点の線分に割る（★向きの判定を線分ごとに行うため）。"""
    import geopandas as gpd
    from shapely.geometry import LineString

    segs, rnk, brg = [], [], []
    for geom, w in zip(gsi.geometry, gsi["vt_rnkwidth"], strict=True):
        c = np.asarray(geom.coords)
        for i in range(len(c) - 1):
            segs.append(LineString([c[i], c[i + 1]]))
            rnk.append(w)
            brg.append(bearing(c[i, 0], c[i, 1], c[i + 1, 0], c[i + 1, 1]))
    return gpd.GeoDataFrame({"rnk": rnk, "gbear": brg}, geometry=segs, crs=epsg)


def assign(edges_m, gsi_m, epsg: int, progress=None) -> tuple[np.ndarray, dict]:
    """各エッジに幅員区分を割り当てる。合わなかったエッジは `None`。

    `edges_m` / `gsi_m` は**同じ平面直角座標系**であること（距離を m で測るため）。
    返り値は長さ `len(edges_m)` の object 配列と、統計の dict。
    """
    import geopandas as gpd
    import pandas as pd
    from shapely.geometry import Point

    n = len(edges_m)
    out = np.full(n, None, dtype=object)
    if not n:
        return out, {"edges": 0, "matched": 0, "matched_share": 0.0}

    sg = _segments(gsi_m, epsg)
    if progress:
        progress(f"RdCL の線分 {len(sg):,}")

    coords = [np.asarray(g.coords) for g in edges_m.geometry]
    mid = np.array(
        [np.asarray(g.interpolate(0.5, normalized=True).coords)[0] for g in edges_m.geometry]
    )
    e_bear = np.array([bearing(c[0, 0], c[0, 1], c[-1, 0], c[-1, 1]) for c in coords])
    pts = gpd.GeoDataFrame(
        {"bear": e_bear, "row": np.arange(n)},
        geometry=[Point(p) for p in mid],
        crs=epsg,
    )

    j = gpd.sjoin_nearest(pts, sg, max_distance=MAX_SNAP_M, distance_col="dist", how="left")
    j = j[~j.index.duplicated(keep="first")]  # 同距離の重複を1つに
    hit = j["rnk"].notna()
    ok = hit & (bearing_diff(j["bear"].to_numpy(), j["gbear"].to_numpy()) <= MAX_BEARING_DEG)
    ok &= j["rnk"].isin(RNKWIDTH_ORDER)
    rows = j.loc[ok, "row"].to_numpy()
    out[rows] = j.loc[ok, "rnk"].to_numpy()

    dist = j.loc[ok, "dist"]
    stats = {
        "edges": n,
        "near": int(hit.sum()),
        "matched": int(ok.sum()),
        "matched_share": float(ok.sum() / n),
        "snap_median_m": float(dist.median()) if len(dist) else float("nan"),
        "snap_p90_m": float(dist.quantile(0.9)) if len(dist) else float("nan"),
        "by_class": pd.Series(out[rows]).value_counts().to_dict(),
    }
    return out, stats


def to_narrowness(rnk: np.ndarray, fallback: np.ndarray) -> np.ndarray:
    """区分 → 狭さ。区分が付かなかったエッジは `fallback`（道路種別の近似）を使う。

    ★**0 で埋めない。** 0 は「最も広い道」を意味するので、
      不明を 0 にすると**広い道として優先されてしまう**（T-09 と同じ筋）。
    """
    table = narrowness_table()
    out = np.asarray(fallback, dtype=np.float64).copy()
    for i, k in enumerate(rnk):
        if k is not None:
            out[i] = table[k]
    return out
