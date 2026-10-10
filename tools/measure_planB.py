"""案B（標高を実行時にブラウザが取る）が成立するかを、藤沢版の実データで測る。

★測りたいこと:
  経路探索は「最終経路」ではなく「探索で触った範囲」全部のリスクを要る。
  Δh は半径 250m の近傍最小値なので、触ったノードの周囲 250m の
  標高タイルが要ることになる。
  → 「最終経路ぶん」と「探索範囲ぶん」で必要タイル数がどれだけ違うかを測る。

実測済みの前提（このスクリプトの外で測った値）:
  DEM5A(z15) PNG タイル 335枚の実サイズ = 中央値 66KB / p90 83KB
"""
from __future__ import annotations

import heapq
import math
import sys

import geopandas as gpd
import numpy as np
import pandas as pd

sys.path.insert(0, "src")
from kansui.validation import _sample_pairs, build_graph, edge_cost  # noqa: E402

Z = 15                      # DEM5A を叩くズームレベル（CLAUDE.md の表）
DELTA_H_RADIUS_M = 250.0    # Δh の大きいほうの半径
TILE_KB_MEDIAN = 66         # 実測（dem5a_png 335枚）
THRESHOLD = 0.2449          # 再校正後の点灯閾値
LAM = 8.0                   # λ の既定


def lonlat_to_tile(lon: float, lat: float, z: int = Z) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    la = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(la)) / math.pi) / 2.0 * n)
    return x, y


def tiles_for_points(lonlat: np.ndarray, radius_m: float) -> set[tuple[int, int]]:
    """点群の周囲 radius_m をカバーする z15 タイルの集合。

    ★緯度で1px の実距離が変わるので、各点の緯度で度に直す。
    """
    out: set[tuple[int, int]] = set()
    for lon, lat in lonlat:
        dlat = radius_m / 111_320.0
        dlon = radius_m / (111_320.0 * math.cos(math.radians(lat)))
        x0, y0 = lonlat_to_tile(lon - dlon, lat + dlat)
        x1, y1 = lonlat_to_tile(lon + dlon, lat - dlat)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                out.add((x, y))
    return out


def dijkstra_settled(g, src, dst, lam: float, threshold: float):
    """src→dst の Dijkstra。★確定（settle）したノードを全部記録する。

    これが「ブラウザが risk を知っている必要のある範囲」。
    """
    dist = {src: 0.0}
    prev: dict = {}
    settled: set = set()
    pq = [(0.0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if u in settled:
            continue
        settled.add(u)
        if u == dst:
            break
        for v, data in g[u].items():
            if v in settled:
                continue
            nd = d + edge_cost(data, lam, threshold)
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    if dst not in settled:
        return None, settled
    path = [dst]
    while path[-1] != src:
        path.append(prev[path[-1]])
    return path[::-1], settled


def main() -> None:
    raw = "data/raw"
    edges = gpd.read_file(f"{raw}/roads/road_edges.gpkg", layer="edges")
    risk = pd.read_parquet(f"{raw}/edges_risk.parquet")
    nodes = gpd.read_file(f"{raw}/roads/road_nodes.gpkg", layer="nodes")
    boundary = gpd.read_file(f"{raw}/fujisawa_boundary.geojson").to_crs(6677).union_all()

    g = build_graph(edges, risk)

    n6677 = nodes.to_crs(6677)
    inside = n6677.geometry.within(boundary)
    cand = n6677.loc[inside & n6677["id"].isin(g.nodes)]
    xy = np.c_[cand.geometry.x.to_numpy(), cand.geometry.y.to_numpy()]
    ids = cand["id"].to_numpy()

    # ノード id → 経緯度（タイル番号を出すため）
    n4326 = nodes.to_crs(4326)
    ll = dict(zip(n4326["id"].to_numpy(),
                  np.c_[n4326.geometry.x.to_numpy(), n4326.geometry.y.to_numpy()]))

    rows = []
    for band, (lo, hi), n in [("短(1-3km)", (1.0, 3.0), 20),
                              ("中(3-7km)", (3.0, 7.0), 20),
                              ("長(7-15km)", (7.0, 15.0), 20)]:
        pairs = _sample_pairs(np.random.default_rng(20260920), ids, xy, n, lo, hi)
        for src, dst, km in pairs:
            path, settled = dijkstra_settled(g, src, dst, LAM, THRESHOLD)
            if path is None:
                continue
            t_route = tiles_for_points(np.array([ll[i] for i in path]), DELTA_H_RADIUS_M)
            t_expl = tiles_for_points(np.array([ll[i] for i in settled if i in ll]),
                                      DELTA_H_RADIUS_M)
            rows.append({"band": band, "km": km, "n_settled": len(settled),
                         "n_path": len(path),
                         "tiles_route": len(t_route), "tiles_explored": len(t_expl)})

    df = pd.DataFrame(rows)
    print(f"\n標本 {len(df)} 組 / z{Z} / Δh 半径 {DELTA_H_RADIUS_M:.0f}m / "
          f"タイル中央値 {TILE_KB_MEDIAN}KB\n")
    print(f"{'距離帯':<12}{'n':>4}{'直線km':>8}{'確定ノード':>10}"
          f"{'経路ぶん':>9}{'探索ぶん':>9}{'倍率':>7}{'探索MB':>8}")
    for band, sub in df.groupby("band", sort=False):
        mb = sub["tiles_explored"].median() * TILE_KB_MEDIAN / 1024
        print(f"{band:<12}{len(sub):>4}{sub['km'].median():>8.1f}"
              f"{sub['n_settled'].median():>10.0f}"
              f"{sub['tiles_route'].median():>9.0f}{sub['tiles_explored'].median():>9.0f}"
              f"{sub['tiles_explored'].median() / sub['tiles_route'].median():>7.1f}"
              f"{mb:>8.1f}")
    print(f"\n全体: 探索ぶんタイル 中央値 {df['tiles_explored'].median():.0f} / "
          f"p90 {df['tiles_explored'].quantile(0.9):.0f} / 最大 {df['tiles_explored'].max():.0f}")
    print(f"      転送量  中央値 {df['tiles_explored'].median() * TILE_KB_MEDIAN / 1024:.1f}MB / "
          f"p90 {df['tiles_explored'].quantile(0.9) * TILE_KB_MEDIAN / 1024:.1f}MB / "
          f"最大 {df['tiles_explored'].max() * TILE_KB_MEDIAN / 1024:.1f}MB")
    print(f"\n参考: 藤沢市域+3km 全体の DEM5A タイルは 335枚 "
          f"= {335 * TILE_KB_MEDIAN / 1024:.1f}MB")
    df.to_csv("/private/tmp/claude-501/-Users-uchidakoichi-Developer-Kansui/"
              "357dc6f1-08d4-43d9-85c4-ed18d373a2db/scratchpad/planB.csv", index=False)


if __name__ == "__main__":
    main()
