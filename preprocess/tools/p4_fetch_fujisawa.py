"""P-4 その1: 藤沢市域の最適化ベクトルタイル（RdCL）を取得して GeoParquet に落とす。

★絶対ルール3: 逐次・sleep 1.0秒・同時接続1・キャッシュ・UA 明示。
  同じタイルは二度取りに行かない（再実行はキャッシュから返る）。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, box

from kansui.gsi_vector import ROAD_LAYER, VectorTiles, decode_layer

Z = 16  # 最大ズーム。★低ズームを取っても詳細は上がらない
UA = "kansui-national-poc/0.1 (flood-risk routing PoC; +https://github.com/uchidakoichi/KansuiNavi)"
FUJISAWA = Path(__file__).resolve().parents[3] / "Kansui/preprocess/data/raw"  # 藤沢版のリポジトリを隣に置いた前提
OUT = Path("data/raw/gsi_rdcl_fujisawa.parquet")


def tile_bbox(x: int, y: int, z: int):
    n = 2**z
    lon0 = x / n * 360.0 - 180.0
    lon1 = (x + 1) / n * 360.0 - 180.0
    lat0 = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    lat1 = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y + 1) / n))))
    return box(lon0, lat1, lon1, lat0)


def main() -> None:
    boundary = gpd.read_file(FUJISAWA / "fujisawa_boundary.geojson").to_crs(4326)
    poly = boundary.union_all()
    w, s, e, n = boundary.total_bounds

    nn = 2**Z

    def tnum(lon: float, lat: float):
        return (
            int((lon + 180) / 360 * nn),
            int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * nn),
        )

    x0, y0 = tnum(w, n)
    x1, y1 = tnum(e, s)
    targets = [
        (x, y)
        for y in range(y0, y1 + 1)
        for x in range(x0, x1 + 1)
        if tile_bbox(x, y, Z).intersects(poly)
    ]
    print(f"対象タイル: {len(targets)} 枚（z{Z}）", flush=True)

    vt = VectorTiles(cache_dir=Path("data/cache/xyz"), user_agent=UA, sleep_sec=1.0)
    geoms, props = [], []
    for i, (x, y) in enumerate(targets, 1):
        data = vt.fetch(Z, x, y)
        if i % 25 == 0 or i == len(targets):
            print(f"  {i}/{len(targets)}  {vt.stats}", flush=True)
        if data is None:
            continue
        for lines, p in decode_layer(data, Z, x, y, ROAD_LAYER):
            for ln in lines:
                geoms.append(LineString(ln))
                props.append({**p, "tile_x": x, "tile_y": y})

    gdf = gpd.GeoDataFrame(props, geometry=geoms, crs=4326)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_parquet(OUT)
    print(f"\n★RdCL {len(gdf):,} 本 → {OUT}", flush=True)
    print("取得統計:", vt.stats, flush=True)
    print("\nvt_rnkwidth の内訳:", flush=True)
    print(gdf["vt_rnkwidth"].value_counts(dropna=False).to_string(), flush=True)
    print("\nvt_code の内訳:", flush=True)
    print(gdf["vt_code"].value_counts(dropna=False).to_string(), flush=True)


if __name__ == "__main__":
    sys.exit(main())
