"""P-7 の一部: 区画ごとの配布ファイルを実際に作り、経路に要る区画数を測る。

★問い（設計 §5 P-7）: 初回起動の転送量／区画をまたぐ経路の応答時間／メモリ。

★**この スクリプトで測れないこと**をはじめに書く:
  - **実機（iOS Safari / Android Chrome）での時間とメモリ**。
    全国版の web クライアントはまだ無い。ここで出す時間は
    **Python の Dijkstra の時間**であって、ブラウザの TypedArray 実装とは別物。
  - T-13（MapLibre の Worker）/ T-14（圧縮・404）/ T-15（Service Worker）。
    いずれも**配信して実機で見ないと分からない**。

★**測れること**（そしてこれが P-7 の肝）:
  - 区画ごとの**実ファイルサイズ**（P-2 の 2.48MB は 26.1バイト/エッジの**見積もり**だった）
  - 1経路が**いくつの区画を要するか** ＝ 実行時に落とす量
  - 探索の広さ（確定ノード数）。★案B を棄却したときと同じ測り方

★区画への割り当ては**両端ノード**で見る。中点で決めるとまたぐエッジが
  片方の区画にしか無くなって経路が切れる（P-2 と同じ方針）。
"""

from __future__ import annotations

import gzip
import heapq
import logging
import math
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import geopandas as gpd
import numpy as np

from kansui import boundary as B
from kansui.config import Config
from kansui.fetcher import TileFetcher
from kansui.mesh import mesh_code
from kansui.region import Region, region_epsg

ROADS = Path("data/raw/roads")
OUT = Path("data/raw/p7")

#: 藤沢版 graph.bin の実測（2026-09-20）。**見積もりではなく実物**
FUJISAWA_BYTES_PER_EDGE = 26.1
FUJISAWA_GZ_PER_EDGE = 11.9

#: 1エッジ / 1ノードあたりの内訳（藤沢版 graph.bin の layout そのまま）
PER_EDGE_BYTES = 4 + 4 + 2 + 1 + 1 + 1 + 2 + 1 + 1   # U,V,time,score,state,oneway,name,narrow,width
PER_NODE_BYTES = 4 + 4 + 2                            # lon, lat, elev


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("kansui.fetcher").setLevel(logging.WARNING)
    cfg = Config.load()
    region = Region.from_config(cfg.require("region"))
    gdf = B.load(TileFetcher.from_config(cfg), cfg.require("boundary.url_template"),
                 region, cfg.resolve_path("boundary.cache_dir"))
    epsg = region_epsg(gdf)
    level = int(cfg.require("split.level"))
    limit = float(cfg.require("split.max_file_bytes"))

    edges = gpd.read_parquet(ROADS / f"edges_{region.name}.parquet")
    print(f"地域 {region.name} / EPSG:{epsg} / エッジ {len(edges):,}\n", flush=True)

    # --- 区画への割り当て -------------------------------------------------------
    m_from = edges["mesh_from"].to_numpy()
    m_to = edges["mesh_to"].to_numpy()

    # ノードの区画（両端の経緯度から）
    g4326 = edges.to_crs(4326)
    coords = [np.asarray(g.coords) for g in g4326.geometry]
    u = edges["u"].to_numpy()
    v = edges["v"].to_numpy()
    node_mesh: dict[int, str] = {}
    node_xy: dict[int, tuple[float, float]] = {}
    for c, a, b in zip(coords, u, v, strict=True):
        if a not in node_mesh:
            node_mesh[a] = mesh_code(c[0][0], c[0][1], level)
            node_xy[a] = (c[0][0], c[0][1])
        if b not in node_mesh:
            node_mesh[b] = mesh_code(c[-1][0], c[-1][1], level)
            node_xy[b] = (c[-1][0], c[-1][1])

    edges_per_mesh: Counter = Counter()
    for a, b in zip(m_from, m_to, strict=True):
        edges_per_mesh[a] += 1
        if a != b:
            edges_per_mesh[b] += 1          # ★またぐエッジは両方に入れる
    nodes_per_mesh = Counter(node_mesh.values())

    # --- ★実ファイルを作る（見積もりではなく実物） -------------------------------
    OUT.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(20260920)
    rows = []
    for mesh, ne in edges_per_mesh.items():
        nn = nodes_per_mesh.get(mesh, 0)
        # 藤沢版 graph.bin と同じ並び・同じ型で、同じ大きさの実体を書く。
        # ★値そのものは配布物ではないので乱数で埋める。**大きさと圧縮率**を測るのが目的。
        blob = bytearray()
        blob += rng.integers(0, 2**32, nn, dtype=np.uint32).tobytes()          # nodeLon
        blob += rng.integers(0, 2**32, nn, dtype=np.uint32).tobytes()          # nodeLat
        blob += rng.integers(0, nn or 1, ne, dtype=np.uint32).tobytes()        # edgeU
        blob += rng.integers(0, nn or 1, ne, dtype=np.uint32).tobytes()        # edgeV
        blob += rng.integers(0, 3000, ne, dtype=np.uint16).tobytes()           # edgeTime
        blob += rng.integers(0, 256, ne, dtype=np.uint8).tobytes()             # edgeScore
        blob += rng.integers(0, 3, ne, dtype=np.uint8).tobytes()               # edgeState
        blob += rng.integers(0, 2, ne, dtype=np.uint8).tobytes()               # edgeOneway
        blob += rng.integers(0, 1200, ne, dtype=np.uint16).tobytes()           # edgeName
        blob += rng.integers(0, 5, ne, dtype=np.uint8).tobytes()               # edgeNarrow
        blob += rng.integers(0, 256, ne, dtype=np.uint8).tobytes()             # edgeWidth
        blob += rng.integers(-3000, 3000, nn, dtype=np.int16).tobytes()        # nodeElev
        gz = gzip.compress(bytes(blob), 9)
        rows.append({"mesh": mesh, "nodes": nn, "edges": ne,
                     "raw": len(blob), "gz": len(gz)})
    import pandas as pd
    df = pd.DataFrame(rows).sort_values("raw")
    df.to_csv(OUT / "mesh_sizes.csv", index=False)

    print(f"{'=' * 70}\n★区画ごとの**実ファイル**サイズ（{len(df)} 区画）\n{'=' * 70}", flush=True)
    for name, col in (("そのまま", "raw"), ("gzip", "gz")):
        s = df[col] / 1e6
        print(f"  {name:<8} 中央値 {s.median():>6.2f}MB / p90 {s.quantile(.9):>6.2f}MB"
              f" / ★最大 {s.max():>6.2f}MB", flush=True)
    print(f"  合計 そのまま {df['raw'].sum() / 1e6:.1f}MB / gzip {df['gz'].sum() / 1e6:.1f}MB"
          f"（{df['gz'].sum() / df['raw'].sum():.0%}）", flush=True)
    print(f"  ★1ファイル上限 {limit / 1e6:.1f}MB に対し最大 "
          f"{df['raw'].max() / limit:.1%}", flush=True)
    print(f"  ★1エッジあたり {df['raw'].sum() / df['edges'].sum():.1f}バイト "
          f"（藤沢版の実測 {FUJISAWA_BYTES_PER_EDGE}）", flush=True)

    # --- 経路に要る区画数 -------------------------------------------------------
    print(f"\n{'=' * 70}\n★1経路に要る区画数と転送量\n{'=' * 70}", flush=True)
    size_of = dict(zip(df["mesh"], df["gz"], strict=True))

    # CSR を組む（★λ=0 の最速経路で測る。risk はまだ県全域では出していない）
    ids = sorted(node_mesh)
    idx = {n: i for i, n in enumerate(ids)}
    t = np.clip(edges["travel_time_s"].to_numpy(), 0.1, None)
    ow = edges["oneway"].astype(str).isin(["yes", "true", "1"]).to_numpy()
    ui = np.array([idx[a] for a in u])
    vi = np.array([idx[b] for b in v])
    src = np.concatenate([ui, vi[~ow]])     # ★一方通行でない辺だけ逆向きも張る
    dst = np.concatenate([vi, ui[~ow]])
    w = np.concatenate([t, t[~ow]])
    order = np.argsort(src, kind="stable")
    src, dst, w = src[order], dst[order], w[order]
    head = np.searchsorted(src, np.arange(len(ids) + 1))
    print(f"  グラフ: ノード {len(ids):,} / 有向辺 {len(dst):,}", flush=True)

    xy = np.array([node_xy[n] for n in ids])
    # 平面直角座標での距離を使うため一度だけ変換
    pxy = np.array(gpd.GeoSeries(gpd.points_from_xy(xy[:, 0], xy[:, 1]), crs=4326)
                   .to_crs(epsg).apply(lambda p: (p.x, p.y)).tolist())

    def dijkstra(s, d):
        dist = defaultdict(lambda: math.inf)
        dist[s] = 0.0
        settled = set()
        pq = [(0.0, s)]
        while pq:
            dd, x = heapq.heappop(pq)
            if x in settled:
                continue
            settled.add(x)
            if x == d:
                break
            for k in range(head[x], head[x + 1]):
                y = int(dst[k])
                if y in settled:
                    continue
                nd = dd + float(w[k])
                if nd < dist[y]:
                    dist[y] = nd
                    heapq.heappush(pq, (nd, y))
        return (d in settled), settled

    bands = [("短 2-5km", 2, 5, 10), ("中 5-15km", 5, 15, 10), ("長 15-40km", 15, 40, 10)]
    for label, lo, hi, n in bands:
        res = []
        tries = 0
        while len(res) < n and tries < n * 200:
            tries += 1
            i, j = rng.integers(0, len(ids), 2)
            km = float(np.hypot(*(pxy[i] - pxy[j]))) / 1000
            if not (lo <= km <= hi):
                continue
            t0 = time.time()
            ok, settled = dijkstra(int(i), int(j))
            if not ok:
                continue
            ms = {node_mesh[ids[k]] for k in settled}
            res.append({"km": km, "settled": len(settled), "meshes": len(ms),
                        "mb": sum(size_of.get(m, 0) for m in ms) / 1e6,
                        "sec": time.time() - t0})
        if not res:
            print(f"  {label}: 経路が取れなかった")
            continue
        d2 = pd.DataFrame(res)
        print(f"  {label:<12} n={len(d2):>3}  直線 {d2['km'].median():>5.1f}km  "
              f"確定ノード {d2['settled'].median():>9,.0f}  "
              f"★区画 {d2['meshes'].median():>4.0f}  "
              f"★転送(gz) {d2['mb'].median():>6.1f}MB  "
              f"(Python の Dijkstra {d2['sec'].median():>5.1f}秒)", flush=True)

    print("\n★上の秒数は **Python の Dijkstra** の時間。ブラウザの実装とは別物。", flush=True)
    print("  実機の時間・メモリ・T-13/T-14/T-15 は、"
          "web クライアントを作って配信しないと測れない。", flush=True)
    print(f"→ {OUT}/mesh_sizes.csv", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
