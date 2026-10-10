"""NK-09: 位置不明の冠水想定箇所を、**説明表の文字と OSM の道路の名前**から推す（2026-10-09・利用者判断「位置不明を減らす」）。

★LLM・画像照合を使わない。手掛かりは文字だけ:
  地域   … 説明表の市区町村・字丁目（`locate_pdfs.parse_one`。places.json の地名と一致したもの）の代表点の周り
  道路   … 路線（国道の番号・県道などの「◯◯線」）と、施設名（◯◯トンネル・◯◯アンダーパス など）を
             OSM の道路（name・ref・tags）と突き合わせる。アンダーパスと書かれていれば立体交差の下の区間に絞る
  位置   … 当たった区間の中点の中央値。当たりが狭い範囲に固まったときだけ採る（広がっていれば位置不明のまま）
★採った点は**要確認（円）だけ**にする。経路計算には効かせない（確定は公表座標・地図の照合で決めたものだけ）。

★**答え合わせができる。** 整備局が座標（KML）を公表している箇所は、同じ様式の説明表がある。
  その座標を隠して同じ手順で推し、当たった割合・ずれ・円に入る割合を測る（`validate`）。
  さらに**帰無検査**として、路線・施設名を同じ都道府県の別の箇所と入れ替えて推し、偶然の当たりを測る。

使い方（preprocess/ で）:
  uv run --with pymupdf python tools/nk09_name/locate_by_name.py parse      # 説明表を全部読む（数分）
  uv run --with pymupdf python tools/nk09_name/locate_by_name.py validate   # 答え合わせと帰無検査
  uv run --with pymupdf python tools/nk09_name/locate_by_name.py apply      # 位置不明の行を CSV で書き換える
★`build_points_webmap.py` を回し直すと CSV の Webマップの行が作り直されるので、そのあとにもう一度 `apply` する。
"""

from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]  # preprocess/
sys.path.insert(0, str(ROOT / "tools/nk09_webmap"))
import locate_pdfs as LP  # noqa: E402

PDF_DIR = ROOT / "data/cache/nk09_pdf"
WORK = ROOT / "data/raw/nk09_name"
CSV_PATH = ROOT / "data/manual/road_kansui_points.csv"
FINAL = ROOT / "data/raw/final"
ROADS = ROOT / "data/raw/roads"

# ★自主基準（公式の根拠は無い）。`validate` の結果を見て決めた値。決めたら config.yaml へ移す
SPREAD_MAX_M = 300      # 当たった区間の中点が、中央値からこの距離（90%点）に収まれば採る
RADIUS_MIN_M = 150      # 円の半径の下限（Webマップで市区町村が合わない点の円と同じ）
RADIUS_MAX_M = 400      # 上限（関東の全域図から推した円と同じ）

DISTRICT_NEAR_M = 500   # 推した位置が字丁目の代表点からこれ以内のときだけ採る（答え合わせで 73%→89%）
UNDER_NEAR_M = 80       # 「◯◯の下のアンダーパス」: 当たった道路からこの距離の立体交差の下を採る
UNDER = re.compile(r"アンダー|ガード|高架下|くぐ|地下道|立体交差|線下|の下|ボックス|BOX|函渠|カルバート", re.I)
#: 施設名として役に立たない語（これだけでは場所が決まらない）
GENERIC = {"アンダーパス", "アンダー", "ガード", "トンネル", "地下道", "函渠", "交差点", "橋", "高架下"}
NUM = str.maketrans("０１２３４５６７８９", "0123456789")


def _routes(route: str, kind: str) -> tuple[set[str], set[str], list[str]]:
    """路線の欄から (国道の番号, 都道府県道の番号, 「◯◯線」の名前) を取り出す"""
    t = LP.nz(f"{kind} {route}").translate(NUM)
    nat, pref = set(), set()
    if "国道" in t:
        nat |= set(re.findall(r"国道(\d+)", t))
        if not nat and re.search(r"国道", LP.nz(kind)):
            nat |= set(re.findall(r"(\d+)号", t))
    elif re.search(r"[都道府県]道|主要地方道", t):
        pref |= set(re.findall(r"(\d+)号(?!線)", t))
    names = []
    for m in re.finditer(r"([^\s（）()・、]{2,}?線)", re.sub(r"[（(][^）)]*[）)]", " ", t)):
        n = re.sub(r"^(主要地方道|一般県道|一般府道|一般道道|一般都道|県道|府道|都道|道道|市道|町道|村道|区道|主|一)", "", m.group(1))
        if len(n) >= 3 and not re.fullmatch(r"[\d号]+線", n):
            names.append(n)
    return nat, pref, names


def _facilities(fac: str) -> list[str]:
    """施設名（◯◯トンネル・◯◯アンダーパス・◯◯橋 など）。場所を決められる長さのものだけ"""
    out = []
    for w in re.split(r"[\s　・、/／（）()「」]+", LP.nz(fac)):
        w = re.sub(r"(付近|周辺|地内|地先|先)$", "", w)
        # ★「◯◯橋下アンダーパス」→「◯◯橋」。アンダーパスそのものの名前は OSM にまず無い
        w = re.sub(r"(下?(アンダーパス|アンダー|ガード)|下)$", "", w)
        if len(w) >= 3 and w not in GENERIC and not re.fullmatch(r"[\d\-ー号線]+", w):
            out.append(w)
    return out


# ── 1. 説明表を読む ─────────────────────────────────────────────────
def cmd_parse() -> int:
    M, by_muni = LP.load_places()
    kml = json.load(open(ROOT / "data/raw/nk09_webmap/kml_points.json"))
    truth = defaultdict(list)
    for k in kml:
        if k.get("pdf"):
            truth[Path(k["pdf"]).name].append([k["lon"], k["lat"]])
    rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8")))
    unloc = {Path(r["src_url"]).name for r in rows if r["tier"] == "unlocated" and r["src_url"].endswith(".pdf")
             and r["method"].startswith("整備局")}
    out = []
    files = sorted(PDF_DIR.glob("*/*.pdf"))
    for n, f in enumerate(files):
        try:
            rec = LP.parse_one(f, M, by_muni)
        except Exception as e:  # noqa: BLE001 ★壊れた PDF は数えて落とす（黙って捨てない）
            rec = {"file": str(f.relative_to(PDF_DIR)), "err": f"読めない: {e}"}
        rec.pop("_t", "")
        # ★北陸の様式は「道路名国道8号」「施設名◯◯」と1行に書く（表の欄ではない）。
        #   `parse_one` は表の欄の下を読むので、この様式では住所・警察署を拾っていた → こちらで上書きする
        import pymupdf

        raw = pymupdf.open(f)[0].get_text()
        if "道路名" in raw and "整理番号" in raw:
            for key, lab in (("route", "道路名"), ("facility", "施設名")):
                m = re.search(lab + r"[ \t　]*\n?[ \t　]*([^\n]+)", raw)
                v = m.group(1).strip() if m else ""
                rec[key] = "" if v in ("施設名", "地　　先　　名") else v
            rec["kind"] = ""
        # ★アンダーパスかどうかは**路線・施設名の欄だけ**で見る（本文全体だと様式の決まり文句に当たる）
        rec["under"] = bool(UNDER.search(LP.nz(f"{rec.get('route') or ''} {rec.get('facility') or ''}")))
        rec["pdf"] = f.name
        rec["truth"] = truth.get(f.name, [])
        rec["unlocated"] = f.name in unloc
        out.append(rec)
        if n % 500 == 0:
            print(f"{n}/{len(files)}", file=sys.stderr)
    WORK.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(WORK / "parsed_all.json", "w"), ensure_ascii=False)
    c = Counter((bool(r["truth"]), r["unlocated"]) for r in out)
    print(f"説明表 {len(out)} / 座標あり {c[(True, False)]} / 位置不明 {c[(False, True)]} / どちらでもない {c[(False, False)]}")
    return 0


def _kanto_recs() -> list[dict]:
    """関東・山梨の位置不明の行（PDF の説明表が無い）。路線・場所の欄から同じ形の記録を作る"""
    M, by_muni = LP.load_places()
    recs = []
    for r in csv.DictReader(open(CSV_PATH, encoding="utf-8")):
        if r["tier"] != "unlocated" or r["method"].startswith("整備局"):
            continue
        place = r["place"]
        t = LP.nz(place)
        munis = [i for i, ms in enumerate(M) if LP.nz(ms.replace(" ", "")) in LP.nz(place.replace(" ", ""))
                 or (r["region"] == "yamanashi" and ms.startswith("山梨県 ") and LP.nz(ms.partition(" ")[2]) in t)]
        best = None
        for i in munis:
            for key, name, lo, la in by_muni.get(i, []):
                if len(key) >= 2 and key in t and (best is None or len(key) > len(best[0])):
                    best = (key, name, lo, la)
        rec = {"id": r["id"], "muni": [M[i] for i in munis], "route": r["route"], "kind": "", "facility": place,
               "under": bool(UNDER.search(place)), "truth": [], "unlocated": True, "csv_id": r["id"]}
        if best:
            rec.update(district=best[1], center=[best[2], best[3]], radius_m=LP.R_DISTRICT_M)
        elif munis:
            pts = [(lo, la) for i in munis for _, _, lo, la in by_muni.get(i, [])]
            if pts:
                rec.update(center=[float(np.median([q[0] for q in pts])), float(np.median([q[1] for q in pts]))],
                           radius_m=LP.R_MUNI_M)
        recs.append(rec)
    return recs


# ── 2. 道路 ─────────────────────────────────────────────────────────
class Roads:
    """1地域ぶんの道路（区間の中点・名前・路線番号・道路の種類・立体交差の下か）"""

    def __init__(self, region: str):
        import pandas as pd
        from scipy.spatial import cKDTree

        f = pd.read_parquet(FINAL / f"{region}.parquet", columns=["way", "lon0", "lat0", "lon1", "lat1", "state"])
        e = pd.read_parquet(ROADS / f"edges_{region}.parquet", columns=["id", "highway", "name", "ref", "tunnel", "layer", "tags"])
        e = e.drop_duplicates("id").set_index("id")
        self.lon = ((f.lon0 + f.lon1) / 2).to_numpy()
        self.lat = ((f.lat0 + f.lat1) / 2).to_numpy()
        way = f.way.to_numpy()
        ok = np.isin(way, e.index.to_numpy())
        self.lon, self.lat, way, state = self.lon[ok], self.lat[ok], way[ok], f.state.to_numpy()[ok]
        ee = e.loc[way]
        self.highway = ee.highway.fillna("").to_numpy()
        self.name = np.array([LP.nz(x) for x in ee.name.fillna("")], dtype=object)
        self.ref = ee.ref.fillna("").astype(str).to_numpy()
        self.tags = np.array([LP.nz(x) for x in ee.tags.fillna("")], dtype=object)
        low = (ee.tunnel.fillna("").isin(["yes", "building_passage", "culvert"]).to_numpy()
               | ee.layer.fillna("0").astype(str).str.startswith("-").to_numpy())
        self.under = (state == "underpass") | low
        self.lat0 = float(np.mean(self.lat)) if len(self.lat) else 35.0
        self.k = np.cos(np.radians(self.lat0))
        self.tree = cKDTree(np.c_[self.lon * self.k * 111320, self.lat * 110540])
        self.box = (self.lon.min(), self.lat.min(), self.lon.max(), self.lat.max())

    def near(self, lon: float, lat: float, r_m: float) -> np.ndarray:
        return np.array(self.tree.query_ball_point([lon * self.k * 111320, lat * 110540], r_m), dtype=np.int64)


def _km(a, b) -> float:
    k = np.cos(np.radians((a[1] + b[1]) / 2))
    return float(np.hypot((a[0] - b[0]) * k * 111.32, (a[1] - b[1]) * 110.54))


def locate(rec: dict, R: Roads) -> dict:
    """1か所を推す。返す: {ok, lon, lat, spread_m, by, n} か {ok: False, why}"""
    if not rec.get("center"):
        return {"ok": False, "why": "市区町村・字丁目が読めない"}
    idx = R.near(rec["center"][0], rec["center"][1], rec.get("radius_m", LP.R_DISTRICT_M))
    if not len(idx):
        return {"ok": False, "why": "近くに道路が無い"}
    nat, pref, names = _routes(rec.get("route") or "", rec.get("kind") or "")
    facs = _facilities(rec.get("facility") or "")
    hit_f = np.zeros(len(idx), bool)
    for w in facs:
        hit_f |= np.array([w in R.name[i] or w in R.tags[i] for i in idx])
    hit_r = np.zeros(len(idx), bool)
    for n in nat:
        hit_r |= np.array([R.name[i] == f"国道{n}号" or (n in R.ref[i].split(";") and R.highway[i].startswith("trunk"))
                           for i in idx])
    for n in pref:
        hit_r |= np.array([n in R.ref[i].split(";") and R.highway[i].startswith(("primary", "secondary"))
                           for i in idx])
    for n in names:
        hit_r |= np.array([n in R.name[i] for i in idx])
    if hit_f.any():
        sel, by = hit_f, "施設名"
        if hit_r.any() and (hit_f & hit_r).any():
            sel, by = hit_f & hit_r, "施設名＋路線"
    elif hit_r.any():
        sel, by = hit_r, "路線"
    else:
        return {"ok": False, "why": "路線・施設名が道路に見つからない"}
    j = idx[sel]
    if rec.get("under"):
        # ★当たった道路の近く（`UNDER_NEAR_M`）の立体交差の下。「◯◯橋の下」は橋そのものが当たるので、その下を採る
        from scipy.spatial import cKDTree

        u = idx[R.under[idx]]
        if len(u):
            t = cKDTree(np.c_[R.lon[j] * R.k * 111320, R.lat[j] * 110540])
            dd, _ = t.query(np.c_[R.lon[u] * R.k * 111320, R.lat[u] * 110540])
            u = u[dd <= UNDER_NEAR_M]
        if not len(u):
            return {"ok": False, "why": "アンダーパスと書かれているが、当たった道路の近くに立体交差の下が無い"}
        j, by = u, by + "＋立体交差の下"
    c = [float(np.median(R.lon[j])), float(np.median(R.lat[j]))]
    d = np.array([_km(c, [R.lon[x], R.lat[x]]) for x in j]) * 1000
    spread = float(np.percentile(d, 90))
    if spread > SPREAD_MAX_M:
        return {"ok": False, "why": f"当たりが広がっている（{spread:.0f}m）", "by": by}
    if not rec.get("district"):
        return {"ok": False, "why": "字丁目が読めない（市区町村だけでは広すぎる）", "by": by}
    if _km(c, rec["center"]) * 1000 > DISTRICT_NEAR_M:
        return {"ok": False, "why": f"字丁目の代表点から遠い（{_km(c, rec['center']) * 1000:.0f}m）", "by": by}
    return {"ok": True, "lon": c[0], "lat": c[1], "spread_m": spread, "by": by, "n": int(len(j))}


def _regions_for(recs: list[dict]) -> dict[str, list[int]]:
    """記録ごとに、代表点を含む地域（final の範囲）を割り当てる"""
    import pyarrow.parquet as pq

    out = defaultdict(list)
    for f in sorted(FINAL.glob("*.parquet")):
        t = pq.read_table(f, columns=["lon0", "lat0"])
        lo, la = t["lon0"].to_numpy(), t["lat0"].to_numpy()
        box = (lo.min(), la.min(), lo.max(), la.max())
        for i, r in enumerate(recs):
            c = r.get("center")
            if c and box[0] <= c[0] <= box[2] and box[1] <= c[1] <= box[3]:
                out[f.stem].append(i)
    return out


def _run(recs: list[dict]) -> list[dict]:
    """全記録を地域ごとに推す。代表点が複数の地域に入るときは、当たったものを採る"""
    res: list[dict] = [{"ok": False, "why": "どの地域にも入らない"} for _ in recs]
    for reg, ids in sorted(_regions_for(recs).items()):
        R = Roads(reg)
        for i in ids:
            r = locate(recs[i], R)
            if r["ok"] or not res[i]["ok"]:
                if r["ok"] or res[i]["why"] == "どの地域にも入らない":
                    res[i] = r
        print(f"{reg}: {len(ids)} 件", file=sys.stderr)
    return res


# ── 3. 答え合わせ ───────────────────────────────────────────────────
def _score(recs, res, label):
    n = len(recs)
    ok = [(r, x) for r, x in zip(recs, res) if x["ok"]]
    err = [min(_km([x["lon"], x["lat"]], t) for t in r["truth"]) * 1000 for r, x in ok]
    rad = [min(RADIUS_MAX_M, max(RADIUS_MIN_M, x["spread_m"])) for _, x in ok]
    inside = sum(e <= q for e, q in zip(err, rad))
    print(f"\n[{label}] 対象 {n} / 推せた {len(ok)}（{len(ok) / max(n, 1):.1%}）")
    if ok:
        e = np.array(err)
        print(f"  ずれ 中央値 {np.median(e):.0f}m / 90%点 {np.percentile(e, 90):.0f}m / 最大 {e.max():.0f}m")
        print(f"  ★円（半径 {RADIUS_MIN_M}〜{RADIUS_MAX_M}m）に入った {inside}/{len(ok)}（{inside / len(ok):.1%}）")
        print("  手掛かり別:", dict(Counter(x["by"] for _, x in ok)))
        by = defaultdict(list)
        for (r, x), e1, q in zip(ok, err, rad):
            by[x["by"]].append(e1 <= q)
        print("  手掛かり別の円に入った割合:", {k: f"{sum(v)}/{len(v)}" for k, v in by.items()})
    print("  推せなかった理由:", dict(Counter(x["why"] for x in res if not x["ok"]).most_common(6)))
    return inside, len(ok)


def cmd_validate() -> int:
    allr = json.load(open(WORK / "parsed_all.json"))
    recs = [r for r in allr if r.get("truth") and len(r["truth"]) == 1]
    res = _run(recs)
    _score(recs, res, "答え合わせ（公表座標を隠して推す）")
    json.dump([{**{k: r.get(k) for k in ("pdf", "center", "radius_m", "truth", "under", "district")}, **x}
               for r, x in zip(recs, res)], open(WORK / "validate_result.json", "w"), ensure_ascii=False)
    if "--no-null" in sys.argv:
        return 0
    # ★帰無検査: 路線・施設名・アンダーパスの印を、同じ都道府県の別の箇所と入れ替える
    rng = np.random.default_rng(20261009)
    by_pref = defaultdict(list)
    for i, r in enumerate(recs):
        by_pref[(r.get("muni") or ["?"])[0].split(" ")[0]].append(i)
    shuf = [dict(r) for r in recs]
    for ids in by_pref.values():
        perm = rng.permutation(ids)
        for a, b in zip(ids, perm):
            for k in ("route", "kind", "facility", "under"):
                shuf[a][k] = recs[b].get(k)
    res0 = _run(shuf)
    _score(shuf, res0, "★帰無検査（路線・施設名を同じ都道府県の別の箇所と入れ替え）")
    return 0


# ── 4. 位置不明の行を書き換える ───────────────────────────────────────
def cmd_apply() -> int:
    allr = json.load(open(WORK / "parsed_all.json"))
    recs = [r for r in allr if r.get("unlocated")] + _kanto_recs()
    res = _run(recs)
    rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8")))
    key = {}
    for r, x in zip(recs, res):
        key[r.get("csv_id") or r["pdf"]] = (r, x)
    changed = Counter()
    for row in rows:
        if row["tier"] != "unlocated":
            continue
        k = row["id"] if row["id"] in key else Path(row["src_url"]).name
        if k not in key:
            continue
        r, x = key[k]
        if not x["ok"]:
            changed["位置不明のまま: " + x["why"].split("（")[0]] += 1
            continue
        row.update(
            tier="uncertain", lon=round(x["lon"], 6), lat=round(x["lat"], 6),
            radius_m=int(round(min(RADIUS_MAX_M, max(RADIUS_MIN_M, x["spread_m"])))),
            check_hint=(row["check_hint"] + "。" if row["check_hint"] else "") +
                       f"★円は説明表の文字（{x['by']}）から本アプリが推した位置",
            method=row["method"] + f" ＋ 説明表の文字と OSM の道路名の照合（{x['by']}・本アプリの推定）",
        )
        changed["要確認にした: " + x["by"]] += 1
    if "--dry-run" in sys.argv:
        for k, v in sorted(changed.items()):
            print(f"{v:5} {k}")
        return 0
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    for k, v in sorted(changed.items()):
        print(f"{v:5} {k}")
    print("段", Counter(r["tier"] for r in rows))
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.exit({"parse": cmd_parse, "validate": cmd_validate, "apply": cmd_apply}.get(cmd, lambda: print(__doc__) or 2)())
