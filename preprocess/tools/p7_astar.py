"""★P-7 の追加測定: A* にすると経路に要る区画がどれだけ減るか。

藤沢版の router.worker.ts は**素の Dijkstra**（A* の実装は無い）。
市域+3km ならグラフ全体がメモリに載るので困らなかったが、
全国版は「経路に要る区画だけ読む」ので、**探索の広さがそのまま転送量になる。**

ヒューリスティックは「目的地までの直線距離 ÷ 最高速度」。
★最高速度は config の motorway 100km/h。どの辺もこれより速くないので
  **許容的**（真の残りコストを超えない）＝ 最短経路を保ったまま探索が狭まる。
"""

import heapq
import math
import time
from collections import defaultdict

import geopandas as gpd
import numpy as np
import pandas as pd

from kansui import boundary as B
from kansui.config import Config
from kansui.fetcher import TileFetcher
from kansui.mesh import mesh_code
from kansui.region import Region, region_epsg

cfg = Config.load()
region = Region.from_config(cfg.require("region"))
gdf = B.load(
    TileFetcher.from_config(cfg),
    cfg.require("boundary.url_template"),
    region,
    cfg.resolve_path("boundary.cache_dir"),
)
epsg = region_epsg(gdf)
level = int(cfg.require("split.level"))
VMAX = float(cfg.require("osm.default_speed_kmh")["motorway"]) / 3.6  # m/s

e = gpd.read_parquet("data/raw/roads/edges_kanagawa.parquet")
g4 = e.to_crs(4326)
coords = [np.asarray(g.coords) for g in g4.geometry]
u = e["u"].to_numpy()
v = e["v"].to_numpy()
nm = {}
nxy = {}
for c, a, b in zip(coords, u, v, strict=True):
    if a not in nm:
        nm[a] = mesh_code(c[0][0], c[0][1], level)
        nxy[a] = (c[0][0], c[0][1])
    if b not in nm:
        nm[b] = mesh_code(c[-1][0], c[-1][1], level)
        nxy[b] = (c[-1][0], c[-1][1])
ids = sorted(nm)
idx = {n: i for i, n in enumerate(ids)}
mesh_of = np.array([nm[n] for n in ids])
t = np.clip(e["travel_time_s"].to_numpy(), 0.1, None)
ow = e["oneway"].astype(str).isin(["yes", "true", "1"]).to_numpy()
ui = np.array([idx[a] for a in u])
vi = np.array([idx[b] for b in v])
src = np.concatenate([ui, vi[~ow]])
dst = np.concatenate([vi, ui[~ow]])
w = np.concatenate([t, t[~ow]])
o = np.argsort(src, kind="stable")
src, dst, w = src[o], dst[o], w[o]
head = np.searchsorted(src, np.arange(len(ids) + 1))
xy = np.array([nxy[n] for n in ids])
P = np.array(
    gpd.GeoSeries(gpd.points_from_xy(xy[:, 0], xy[:, 1]), crs=4326)
    .to_crs(epsg)
    .apply(lambda p: (p.x, p.y))
    .tolist()
)
sz = pd.read_csv("data/raw/p7/mesh_sizes.csv")
size_of = dict(zip(sz["mesh"].astype(str), sz["gz"], strict=True))


def search(s, d, astar):
    dist = defaultdict(lambda: math.inf)
    dist[s] = 0.0
    settled = set()
    px, py = P[d]
    h = (lambda x: math.hypot(P[x][0] - px, P[x][1] - py) / VMAX) if astar else (lambda x: 0.0)
    pq = [(h(s), 0.0, s)]
    while pq:
        _, dd, x = heapq.heappop(pq)
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
                heapq.heappush(pq, (nd + h(y), nd, y))
    return (d in settled), settled, dist.get(d, math.inf)


rng = np.random.default_rng(20260920)
print(
    f"{'距離帯':<12}{'n':>4}{'直線km':>8}{'':>3}{'確定ノード':>11}{'区画':>6}{'転送gz':>9}{'秒':>7}"
)
print("-" * 70)
for label, lo, hi, n in [("短 2-5km", 2, 5, 8), ("中 5-15km", 5, 15, 8), ("長 15-40km", 15, 40, 8)]:
    rows = []
    tries = 0
    while len(rows) < n and tries < n * 300:
        tries += 1
        i, j = rng.integers(0, len(ids), 2)
        km = float(np.hypot(*(P[i] - P[j]))) / 1000
        if not (lo <= km <= hi):
            continue
        r = {}
        for tag, astar in (("D", False), ("A*", True)):
            t0 = time.time()
            ok, st, cost = search(int(i), int(j), astar)
            el = time.time() - t0
            if not ok:
                r = None
                break
            ms = set(mesh_of[list(st)])
            r[tag] = {
                "n": len(st),
                "m": len(ms),
                "mb": sum(size_of.get(m, 0) for m in ms) / 1e6,
                "s": el,
                "cost": cost,
            }
        if r:
            rows.append({"km": km, **r})
    if not rows:
        print(f"{label}: 取れず")
        continue
    for tag in ("D", "A*"):
        d2 = pd.DataFrame([x[tag] for x in rows])
        kmm = np.median([x["km"] for x in rows])
        print(
            f"{label if tag == 'D' else '':<12}{len(d2) if tag == 'D' else '':>4}"
            f"{kmm if tag == 'D' else float('nan'):>8.1f}{tag:>3}"
            f"{d2['n'].median():>11,.0f}{d2['m'].median():>6.0f}"
            f"{d2['mb'].median():>8.1f}MB{d2['s'].median():>7.2f}"
        )
    dD = pd.DataFrame([x["D"] for x in rows])
    dA = pd.DataFrame([x["A*"] for x in rows])
    assert np.allclose(dD["cost"], dA["cost"], rtol=1e-9), "★A* が別の経路を出した（許容的でない）"
    print(
        f"{'':<12}★A* で 確定ノード {1 - dA['n'].median() / dD['n'].median():.0%} 減 / "
        f"区画 {dD['m'].median():.0f}→{dA['m'].median():.0f} / "
        f"転送 {dD['mb'].median():.1f}→{dA['mb'].median():.1f}MB"
    )
