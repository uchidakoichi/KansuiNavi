import sys, json, math, re
import numpy as np, pandas as pd
sys.path.insert(0, '.')
from rows import ROWS
NAT = str(__import__("pathlib").Path(__file__).resolve().parents[3])  # リポジトリの根
P = json.load(open(f"{NAT}/web/public/data/places.json"))
M = P["munis"]
KAN = str.maketrans("一二三四五六七八九", "123456789")
def norm(s): return re.sub(r"[\s（）()]", "", s.replace("丁目", "").translate(KAN).replace("十", "1"))
def district(muni, name):
    hits = [(P["lon"][i] / 1e5, P["lat"][i] / 1e5) for i, (n, m) in enumerate(zip(P["name"], P["m"]))
            if muni in M[m] and norm(n) == norm(name)]
    return hits
f = pd.read_parquet(f"{NAT}/preprocess/data/raw/final/yamanashi.parquet")
u = f[f.state == "underpass"].copy()
u["lon"] = (u.lon0 + u.lon1) / 2; u["lat"] = (u.lat0 + u.lat1) / 2
def dist_m(lo1, la1, lo2, la2):
    return np.hypot((lo2 - lo1) * 111320 * math.cos(math.radians(35.6)), (la2 - la1) * 110570)
R = 1500
out = []
for no, mgr, route, dname, alias, (kind, key), dists, cross in ROWS:
    muni, parts = dists[0], dists[1:]
    pts = [p for d in parts for p in district(muni, d)]
    if not pts:
        out.append(dict(no=no, status="地先が引けない", n=0)); continue
    clo, cla = np.mean([p[0] for p in pts]), np.mean([p[1] for p in pts])
    d = dist_m(clo, cla, u.lon.values, u.lat.values)
    c = u[d < R].copy(); c["d"] = d[d < R]
    if kind == "national":
        c = c[c.ref.fillna("").str.split(";").apply(lambda xs: key in xs)]
    elif kind == "name":
        c = c[c.name.fillna("").str.contains(key)]
    # まとまり（50m 以内の辺は同じ箇所）
    groups = []
    for _, r in c.sort_values("d").iterrows():
        for g in groups:
            if dist_m(g[0], g[1], r.lon, r.lat) < 80: g[2].append(r); break
        else: groups.append([r.lon, r.lat, [r]])
    names = sorted({str(r["name"]) for g in groups for r in g[2]})
    out.append(dict(no=no, kind=kind, district_pts=len(pts), n=len(groups),
                    cands=[(round(g[0], 5), round(g[1], 5), int(min(r.d for r in g[2])), sorted({str(r["name"]) for r in g[2]})[:2]) for g in groups[:4]]))
for o in out: print(o)
json.dump(out, open("locate_out.json", "w"), ensure_ascii=False, default=str)
