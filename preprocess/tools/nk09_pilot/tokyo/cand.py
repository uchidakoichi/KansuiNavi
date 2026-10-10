"""文字（路線・地先名・構造物名）から OSM の候補を出す。位置の決め手には地図の印を後で使う。"""
import json, re, math, unicodedata
import numpy as np, pandas as pd
NAT = str(__import__("pathlib").Path(__file__).resolve().parents[4])  # リポジトリの根
fin = pd.read_parquet(f"{NAT}/preprocess/data/raw/final/tokyo_kanagawa.parquet", columns=["u","v","lon0","lat0","lon1","lat1","name","ref","state"])
ed = pd.read_parquet(f"{NAT}/preprocess/data/raw/roads/edges_tokyo_kanagawa.parquet", columns=["u","v","tunnel","covered"])
fin = fin.merge(ed.drop_duplicates(["u","v"]), on=["u","v"], how="left")
fin["lon"] = (fin.lon0+fin.lon1)/2; fin["lat"] = (fin.lat0+fin.lat1)/2
fin = fin[(fin.lon>138.9)&(fin.lon<140.0)&(fin.lat>35.4)&(fin.lat<35.95)]
fin["struct"] = (fin.state=="underpass") | fin.tunnel.fillna("no").isin(["yes","building_passage","culvert"]) | (fin.covered.fillna("no")=="yes")
fin["nm"] = fin["name"].fillna("").map(lambda s: unicodedata.normalize("NFKC", s))
P = json.load(open(f"{NAT}/web/public/data/places.json")); MU = P["munis"]
KAN = str.maketrans("一二三四五六七八九", "123456789")
def nz(s): return unicodedata.normalize("NFKC", s).replace("丁目","").translate(KAN)
def muni_pts(m):
    ms = m.split("・")
    return [(P["lon"][i]/1e5, P["lat"][i]/1e5, P["name"][i]) for i in range(len(P["name"])) if MU[P["m"][i]].startswith("東京都") and any(x in MU[P["m"][i]] for x in ms)]
SUF = r"(トンネル|アンダーパス|アンダーボックス|アンダー|立体交差|立体|地下道|地下自動車道|架道橋|ガード|隧道|ずい道|陸橋)"
def tokens(place):
    p = unicodedata.normalize("NFKC", place)
    t = re.findall(r"([^\s()（）、]+?" + SUF + r")", p)
    return [x[0] for x in t]
def route_no(kind, route):
    m = re.search(r"(\d+)号", unicodedata.normalize("NFKC", route)); return m.group(1) if m else None
rows = [l.rstrip("\n").split("\t") for l in open("llm_rows.tsv")]
out = {}
k = 111320*math.cos(math.radians(35.68))
for no, muni, kind, route, place in rows:
    mp = muni_pts(muni)
    if not mp: out[no] = dict(err="市区町村が引けない"); continue
    lo = np.array([p[0] for p in mp]); la = np.array([p[1] for p in mp])
    box = (lo.min()-0.01, lo.max()+0.01, la.min()-0.01, la.max()+0.01)
    c = fin[(fin.lon>box[0])&(fin.lon<box[1])&(fin.lat>box[2])&(fin.lat<box[3])]
    # 町丁目（地先名の中の地名）
    chome = [(x, y) for x, y, n in mp if len(n) >= 2 and nz(n) in nz(place)]
    toks = tokens(place); rn = route_no(kind, route) if kind in ("国道","都道") else None
    cand = []
    for t in toks:
        core = re.sub(SUF + "$", "", t)
        if len(core) >= 2:
            hit = c[c.nm.str.contains(re.escape(core)) & c.nm.str.contains(re.escape(t[len(core):][:2]) if t[len(core):] else True)]
            if len(hit): cand.append(("名前", t, hit))
    if rn:
        hit = c[c.struct & c.ref.fillna("").str.split(";").apply(lambda xs: rn in xs)]
        if len(hit): cand.append(("路線番号の構造物", rn, hit))
    if chome:
        cx, cy = np.mean([p[0] for p in chome]), np.mean([p[1] for p in chome])
        d = np.hypot((c.lon-cx)*k, (c.lat-cy)*110570); hit = c[c.struct & (d < 600)]
        if len(hit): cand.append(("町丁目の近くの構造物", "", hit))
    pts = []
    for why, t, h in cand:
        for _, r in h.iterrows(): pts.append((round(r.lon,5), round(r.lat,5), why, str(r["name"])))
    out[no] = dict(tokens=toks, route=rn, chome=len(chome), cands=pts)
json.dump(out, open("cands.json","w"), ensure_ascii=False)
print("候補あり:", sum(1 for v in out.values() if v.get("cands")), "/135")
print("名前で当たった:", sum(1 for v in out.values() if any(p[2]=="名前" for p in v.get("cands",[]))))
