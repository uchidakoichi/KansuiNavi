import json, math, os
HERE = os.environ.get('NK09_WORK', os.path.dirname(os.path.abspath(__file__)))
import numpy as np
C = json.load(open(HERE+"/cands.json")); LM = json.load(open(HERE+"/label_marker.json"))
k = 111320*math.cos(math.radians(35.68))
def fit(src, dst):
    A = np.c_[src, np.ones(len(src))]; X, *_ = np.linalg.lstsq(A, dst, rcond=None); return X
def pred(X, xy): return np.c_[xy, np.ones(len(xy))] @ X
def m(a, b): return np.hypot((a[:,0]-b[:,0])*k, (a[:,1]-b[:,1])*110570)
# 初期: 名前で当たった行（候補の重心）
src, dst = [], []
for no, v in C.items():
    nm = [p for p in v.get("cands", []) if p[2] == "名前"]
    if nm and no in LM:
        src.append([LM[no]["x"], LM[no]["y"]]); dst.append([np.mean([p[0] for p in nm]), np.mean([p[1] for p in nm])])
src, dst = np.array(src), np.array(dst); X = fit(src, dst)
print("初期の点", len(src), "残差 m:", np.round(m(pred(X, src), dst)).astype(int))
for it in range(5):
    rows = []
    for no, v in C.items():
        if no not in LM or not v.get("cands"): continue
        p = pred(X, np.array([[LM[no]["x"], LM[no]["y"]]]))[0]
        cs = np.array([[c[0], c[1]] for c in v["cands"]])
        d = m(cs, np.repeat(p[None], len(cs), 0)); i = int(np.argmin(d))
        rows.append((no, [LM[no]["x"], LM[no]["y"]], cs[i], d[i], v["cands"][i]))
    inl = [r for r in rows if r[3] < 800]
    X = fit(np.array([r[1] for r in inl]), np.array([r[2] for r in inl]))
    res = np.array([r[3] for r in rows]); print(f"反復{it}: 当てはめ {len(inl)} 点 / 残差 中央値 {np.median(res):.0f}m p90 {np.percentile(res,90):.0f}m")
out = {r[0]: dict(lon=float(r[2][0]), lat=float(r[2][1]), resid_m=float(r[3]), why=r[4][2], name=r[4][3]) for r in rows}
# 地図の印から推した位置（要確認の円の中心に使う）。候補の無い行も含めて全行
for no in LM:
    p = pred(X, np.array([[LM[no]["x"], LM[no]["y"]]]))[0]
    out.setdefault(no, dict(resid_m=None)); out[no]["map_lon"] = float(p[0]); out[no]["map_lat"] = float(p[1])
json.dump(out, open(HERE+"/georef_out.json", "w"), ensure_ascii=False)
for t in (100, 200, 300, 500, 800): print(f"  地図の印と {t}m 以内で一致: {sum(1 for r in rows if r[3] < t)}/{len(rows)}")

# ── 偶然の一致の検査: 印と行の対応を入れ替えて同じことをする ──
rng = np.random.default_rng(0); keys = [r[0] for r in rows]; hits = {100: [], 200: [], 300: []}
for s in range(20):
    perm = rng.permutation(keys)
    ds = []
    for no, other in zip(keys, perm):
        p = pred(X, np.array([[LM[other]["x"], LM[other]["y"]]]))[0]
        cs = np.array([[c[0], c[1]] for c in C[no]["cands"]]); ds.append(m(cs, np.repeat(p[None], len(cs), 0)).min())
    for t in hits: hits[t].append(np.mean(np.array(ds) < t))
print("入れ替えたときの一致率（20回の平均）:", {t: f"{np.mean(v):.1%}" for t, v in hits.items()})
