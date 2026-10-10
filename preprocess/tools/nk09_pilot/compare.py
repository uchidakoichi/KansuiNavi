import json, math, sys
import numpy as np, pandas as pd
sys.path.insert(0, ".")
from rows import ROWS
reg = {r["no"]: r for r in json.load(open("register_out.json"))}
for r in json.load(open("register_retry.json")): reg[r["no"]] = r
f = pd.read_parquet(str(__import__("pathlib").Path(__file__).resolve().parents[3] / "preprocess/data/raw/final/yamanashi.parquet"),
                    columns=["lon0", "lat0", "lon1", "lat1", "state", "name", "ref"])
def seg_dist(lo, la, a):
    k = 111320 * math.cos(math.radians(la))
    ax, ay = (a.lon0 - lo) * k, (a.lat0 - la) * 110570; bx, by = (a.lon1 - lo) * k, (a.lat1 - la) * 110570
    dx, dy = bx - ax, by - ay; t = np.clip(-(ax * dx + ay * dy) / np.maximum(dx * dx + dy * dy, 1e-9), 0, 1)
    return np.asarray(np.hypot(ax + t * dx, ay + t * dy))
u = f[f.state == "underpass"]
rows = []
for no, mgr, route, dname, alias, (kind, key), dists, cross in ROWS:
    r = reg[no]; ok = r["score"] >= 0.4 and r["score"] - r["second"] >= 0.2
    d_all = seg_dist(r["lon"], r["lat"], f); d_u = seg_dist(r["lon"], r["lat"], u)
    i = int(np.argmin(d_u)); j = int(np.argmin(d_all))
    rows.append(dict(No=no, 画像の一致=f'{r["score"]:.2f}/{r["second"]:.2f}', 画像で確定=ok,
                     最寄りの道路_m=round(float(d_all[j])), 最寄りの道路=f.iloc[j]["name"] or f.iloc[j]["ref"],
                     最寄りの検出アンダーパス_m=round(float(d_u[i])), その名前=u.iloc[i]["name"] or u.iloc[i]["ref"],
                     PDFの路線名=route, lon=r["lon"], lat=r["lat"]))
df = pd.DataFrame(rows); pd.set_option("display.width", 250); pd.set_option("display.max_columns", 20)
print(df.drop(columns=["lon", "lat"]).to_string(index=False))
df.to_csv("pilot_yamanashi.csv", index=False)
ok = df[df["画像で確定"]]
print(f"\n画像で位置が確定: {len(ok)}/18")
print(f"  そのうち検出済みアンダーパスが 30m 以内: {(ok['最寄りの検出アンダーパス_m'] <= 30).sum()} / 50m 以内: {(ok['最寄りの検出アンダーパス_m'] <= 50).sum()}")
