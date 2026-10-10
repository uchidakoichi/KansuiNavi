"""藤沢版の計算済みデータを区画に割って書き出す（★クライアント実装の試験台）。

★なぜ藤沢のデータを使うのか:
  神奈川県全域の Δh / S_topo はまだ出していない（P-6 で4市ぶんだけ）。
  一方、藤沢版には **177,542本ぶんの S_topo と判定不能の印**が揃っている。
  → **先に仕組みを動かす。** 区画 9枚・またぐエッジ 0.35% あるので、
    F-11（区画の遅延読み込み）と A* の試験台としては十分。
  ★神奈川県全域は、仕組みが動いてから流す。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from kansui import export_mesh as EM
from kansui.mesh import mesh_code

FUJISAWA = Path(__file__).resolve().parents[3] / "Kansui/preprocess/data/raw"  # 藤沢版のリポジトリを隣に置いた前提
OUT = Path("../web/public/data")

#: 藤沢版 config の `routing.road_class_narrowness`（道路種別からの近似）。
#: ★全国版は地理院の幅員区分に統一する決定（NK-18）だが、
#:   まだ神奈川県ぶんを取っていないので、ここでは藤沢版の近似をそのまま運ぶ。
NARROW_BY_HIGHWAY = {
    "living_street": 1.0, "residential": 0.8, "unclassified": 0.6,
    "tertiary": 0.3, "tertiary_link": 0.3, "secondary": 0.15, "secondary_link": 0.15,
    "trunk": 0.05, "trunk_link": 0.05, "primary": 0.0, "primary_link": 0.0,
    "motorway": 0.0, "motorway_link": 0.0,
}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    edges = gpd.read_file(FUJISAWA / "roads" / "road_edges.gpkg", layer="edges").to_crs(4326)
    risk = pd.read_parquet(FUJISAWA / "edges_risk.parquet")
    print(f"藤沢版: エッジ {len(edges):,}", flush=True)

    coords = [np.asarray(g.coords) for g in edges.geometry]
    lon = np.array([[c[0][0], c[-1][0]] for c in coords])
    lat = np.array([[c[0][1], c[-1][1]] for c in coords])
    key = np.c_[edges["u"].to_numpy(), edges["v"].to_numpy()].astype(np.float64)
    m_from = np.array([mesh_code(c[0][0], c[0][1], 2) for c in coords])
    m_to = np.array([mesh_code(c[-1][0], c[-1][1], 2) for c in coords])

    narrow = edges["highway"].map(lambda h: NARROW_BY_HIGHWAY.get(h, 0.6)).to_numpy()

    res = EM.export(
        edges, risk, m_from, m_to, key, lon, lat, narrow,
        out_dir=OUT, region_name="fujisawa",
        extra_meta={
            # ★藤沢版で実測して決めた値をそのまま運ぶ（他地域での妥当性は未検証）
            "lambdaDefault": 8.0,
            "lambdaSteps": [0, 0.5, 1, 1.5, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64],
            "muNarrowDefault": 0.5,
            "calibratedDepthM": 1.407,
            "calibratedThreshold": 0.2449,
            "acceptableTimeRatio": 2.0,
            # ★NK-06 決定（2026-09-20）
            "maxRouteDistanceKm": 40.0,
            "useAstar": True,
            "meshLevel": 2,
        },
    )
    print(f"\n★区画 {res['meshes']} 枚 / 道路名 {res['names']:,} 種", flush=True)
    print(f"  そのまま {res['raw'] / 1e6:.2f}MB / gzip {res['gzip'] / 1e6:.2f}MB "
          f"({res['gzip'] / res['raw']:.0%})", flush=True)
    print(f"  ★1エッジあたり {res['raw'] / 177542:.1f} バイト"
          "（藤沢版 graph.bin は 24.3）", flush=True)
    print(f"→ {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
