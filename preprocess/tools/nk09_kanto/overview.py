"""NK-09 関東: 関東地方整備局「道路冠水注意箇所マップ」（都県ごとの全域図＋一覧の PDF）から位置を起こす（2026-10-03）。

★関東は道路防災情報Webマップ（KML）の仕組みに無い。全域図に番号付きの印があるだけで、座標は公表されていない。
  → 東京の試行（tools/nk09_pilot/tokyo、2017年版）と同じ考え方で、**位置は本アプリの推定**として扱う。
★LLM を使わない（令和8年版は一覧も地図の番号も text layer があり、印はベクタ図形）。
  1 一覧   … text layer の語を表の列に割り当てる（見出しの x 位置で列を決める）
  2 印     … ベクタ図形（凡例の色）の中心。番号（text）を、凡例どおりの色の最寄りの印に対応させる
  3 候補   … 一覧の文字から OSM の候補（構造物名・路線番号の立体交差/トンネル・地先の町丁目の近く）
  4 地理参照 … 印の位置（PDF の座標）→ 経緯度のアフィンを、候補との対応で反復して当てはめる
  5 検査   … 残差（印と候補の距離）・帰無検査（印と行の対応を入れ替えたときの一致率）
  段: 200m 未満 confirmed / 800m 未満 uncertain（円の中心は印から推した位置）/ それ以外 unlocated

使い方（preprocess/ で）: uv run --with pymupdf python tools/nk09_kanto/overview.py tokyo
"""

from __future__ import annotations

import json
import math
import re
import sys
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
NAT = ROOT.parent
PDF = ROOT / "data/cache/nk09_pdf_kanto"
OUT = ROOT / "data/raw/nk09_kanto"

#: 都県ごとの設定。markers: 凡例の色（RGB 0〜1）→ 区分。区分は一覧の「道路種別」で決める
SPECS = {
    "tokyo": dict(
        title="アンダーパス部の道路冠水注意箇所マップ・リスト【東京都】", date="令和8年6月15日時点",
        url="https://www.ktr.mlit.go.jp/ktr_content/content/000752436.pdf",
        publisher="国土交通省 関東地方整備局（東京国道事務所・相武国道事務所）",
        regions=["tokyo_kanagawa"], pref="東京都", map_page=0, list_pages=[1, 2],
        markers={(1.0, 0.0, 0.0): "国道", (0.0, 0.0, 1.0): "国道", (0.2, 0.6, 0.4): "都道", (1.0, 0.6, 0.8): "区市道"},
    ),
    "ibaraki": dict(
        title="道路冠水注意箇所マップ【茨城県】", date="令和8年9月8日更新",
        url="https://www.ktr.mlit.go.jp/ktr_content/content/000015506.pdf",
        publisher="国土交通省 関東地方整備局", regions=["ibaraki"], pref="茨城県", map_page=0, list_pages=[1],
        markers={(1.0, 0.0, 0.0): "*"}, leaders=True,
    ),
    "tochigi": dict(
        title="道路冠水注意箇所マップ【栃木県】", date="令和8年6月1日更新",
        url="https://www.ktr.mlit.go.jp/ktr_content/content/000681500.pdf",
        publisher="国土交通省 関東地方整備局", regions=["tochigi"], pref="栃木県", map_page=0, list_pages=[0],
        markers={(1.0, 0.0, 0.0): "*"}, leaders=True,
        map_clip=(0, 0, 1060, 1503), list_clip=(1060, 0, 2126, 1503),
    ),
    "chiba": dict(
        title="千葉県内におけるアンダーパス部等の道路冠水注意箇所マップ", date="令和8年6月30日更新",
        url="https://www.ktr.mlit.go.jp/ktr_content/content/000752021.pdf",
        publisher="国土交通省 関東地方整備局（千葉国道事務所・首都国道事務所）", regions=["chiba"], pref="千葉県",
        map_page=0, list_pages=[1, 2], markers={(1.0, 0.0, 0.0): "*", (0.0, 0.7, 0.9): "*", (0.0, 0.7, 0.3): "*", (1.0, 0.8, 0.0): "*"},
        # (列, 左端, 右端)。先頭は No の右端の目安。管理者の印の列（国・県・市町村, x 150〜215）は読まない
        list_cols=[("no", 0, 58), ("muni", 58, 100), ("kind", 100, 150), ("route", 220, 365), ("place", 365, 800)],
        leaders=True, map_clip=(40, 60, 560, 645), map_exclude=[(255, 495, 560, 645)],
    ),
    "kanagawa": dict(
        title="道路冠水注意箇所マップ・リスト【神奈川県】", date="令和8年6月1日作成",
        url="https://www.ktr.mlit.go.jp/ktr_content/content/000920809.pdf",
        publisher="国土交通省 関東地方整備局", regions=["tokyo_kanagawa"], pref="神奈川県", map_page=0, list_pages=[1],
        markers={(0.0, 0.0, 1.0): "*", (0.0, 0.5, 0.0): "*", (1.0, 0.0, 0.0): "*", (1.0, 1.0, 0.0): "*",
                 (1.0, 0.0, 1.0): "*", (0.6, 0.3, 0.0): "*", (0.6, 0.2, 0.0): "*", (0.1, 0.3, 0.8): "*", (0.0, 0.5, 0.2): "*"},
        leaders=True,
    ),
}

KAN = str.maketrans("一二三四五六七八九", "123456789")


#: ★康熙部首（⼾ U+2F7E など）は NFKC で旧字（戶）になり、地名の「戸」と一致しない（茨城の「水⼾市」）。寄せる
VAR = str.maketrans({"戶": "戸", "⻑": "長", "⻘": "青", "⻄": "西", "⿓": "竜", "⻤": "鬼", "竈": "釜", "﨑": "崎"})


def nfkc(s): return unicodedata.normalize("NFKC", s if isinstance(s, str) else "").translate(VAR)
def nz(s): return re.sub(r"\s", "", nfkc(s)).replace("丁目", "").translate(KAN)


def kind_class(kind: str) -> str:
    k = nfkc(kind)
    if "国道" in k:
        return "国道"
    if "都道" in k or "県道" in k or "府道" in k or "主要地方道" in k:
        return "都道"
    return "区市道"


# ── 1. 一覧 ─────────────────────────────────────────────────────────
def parse_list_fixed(doc, pages, cols) -> pd.DataFrame:
    """列の境目を設定で固定して読む。★千葉は見出しとデータの列がずれ、見出しの無いページもある"""
    rows = []
    no_max_x = cols[0][2]
    for pi in pages:
        words = doc[pi].get_text("words")
        nos = sorted((y0, y1, int(nfkc(w))) for x0, y0, x1, y1, w, *_ in words
                     if x1 <= no_max_x + 2 and re.fullmatch(r"\d{1,3}", nfkc(w)))
        for y0, y1, no in nos:
            ym = (y0 + y1) / 2
            cell = {c: [] for c, *_ in cols[1:]}
            for x0, wy0, x1, wy1, w, *_ in words:
                if abs((wy0 + wy1) / 2 - ym) > 3.5:
                    continue
                c = next((c for c, a, b in cols[1:] if a <= x0 < b), None)
                if c:
                    cell[c].append((x0, w))
            rows.append({"no": no, **{c: " ".join(w for _, w in sorted(v)) for c, v in cell.items()}})
    return pd.DataFrame(rows).drop_duplicates("no").sort_values("no").reset_index(drop=True)


def parse_list(doc, pages, list_clip=None) -> pd.DataFrame:
    """一覧の表を読む。★1ページに表が左右に2つ並ぶ様式（神奈川）があるので、「No」の見出しごとに表を分ける。
    ★見出しは部分一致（「都道府県名市町村名」が1語になっている様式がある）。"""
    LABELS = (("muni", "市町村"), ("kind", "道路種別"), ("route", "路線名"), ("place", "地先名"))
    rows = []
    for pi in pages:
        words = [w for w in doc[pi].get_text("words")
                 if not list_clip or (list_clip[0] <= w[0] <= list_clip[2] and list_clip[1] <= w[1] <= list_clip[3])]
        starts = sorted(w for w in words if re.fullmatch(r"(新)?No\.?|番号", nfkc(w[4])))
        if not starts:
            continue
        xs = [w[0] for w in starts] + [1e9]
        for ti, st in enumerate(starts):
            lo, hi = st[0] - 5, xs[ti + 1] - 5
            tw = [w for w in words if lo <= w[0] < hi]
            hy = st[3]
            heads = {}
            for x0, y0, x1, y1, w, *_ in tw:
                if abs(y0 - st[1]) > 8:
                    continue
                for key, lab in LABELS:
                    if lab in nfkc(w) and key not in heads:
                        heads[key] = (x0, x1)
            if "place" not in heads:
                continue
            order = sorted(heads.items(), key=lambda kv: kv[1][0])
            bounds = []
            for i, (k, (x0, x1)) in enumerate(order):
                left = (order[i - 1][1][1] + x0) / 2 if i else x0 - 40
                right = (x1 + order[i + 1][1][0]) / 2 if i + 1 < len(order) else hi
                bounds.append((k, left, right))
            nos = sorted([(y0, y1, int(nfkc(w))) for x0, y0, x1, y1, w, *_ in tw
                          if y0 > hy and re.fullmatch(r"\d{1,3}", nfkc(w)) and x0 < order[0][1][0] - 2])
            for y0, y1, no in nos:
                ym = (y0 + y1) / 2
                cell = {c: [] for c, *_ in bounds}
                for x0, wy0, x1, wy1, w, *_ in tw:
                    if abs((wy0 + wy1) / 2 - ym) > 3.5 or x1 < order[0][1][0] - 5:
                        continue
                    c = next((c for c, a, b in bounds if a <= (x0 + x1) / 2 < b), None)
                    if c:
                        cell[c].append((x0, w))
                r = {"no": no, **{c: " ".join(w for _, w in sorted(v)) for c, v in cell.items()}}
                r["muni"] = re.sub(r"^(東京都|神奈川県|埼玉県|千葉県|茨城県|栃木県|群馬県|山梨県|長野県)\s*", "", r.get("muni", ""))
                rows.append(r)
    df = pd.DataFrame(rows)
    for c in ("muni", "kind", "route", "place"):
        if c not in df.columns:
            df[c] = ""
    return df.drop_duplicates("no").sort_values("no").reset_index(drop=True)


# ── 2. 印と番号 ──────────────────────────────────────────────────────
def markers_and_labels(doc, spec):
    p = doc[spec["map_page"]]
    marks = []
    for d in p.get_drawings():
        fill, r = d.get("fill"), d["rect"]
        if not fill or r.width > 20 or r.height > 20 or r.width < 3:
            continue
        key = tuple(round(v, 1) for v in fill)
        cls = spec["markers"].get(key)
        clip = spec.get("map_clip")
        if clip and not (clip[0] <= r.x0 <= clip[2] and clip[1] <= r.y0 <= clip[3]):
            continue
        # ★地図の中の別の枠（問い合わせ先の表の色見本など）は除く
        if any(a <= r.x0 <= c and b <= r.y0 <= e for a, b, c, e in spec.get("map_exclude", ())):
            continue
        if cls:
            marks.append((r.x0 + r.width / 2, r.y0 + r.height / 2, cls))
    # 凡例の中の見本は除く（凡例の見出し語の近く）
    legend = [w for w in p.get_text("words") if "凡例" in w[4]]
    def in_legend(x, y):
        # ★凡例の枠だけを除く（右下ぜんぶを除くと、凡例が上にある図で印が全部消える）
        return bool(legend) and legend[0][0] - 40 <= x <= legend[0][0] + 260 and legend[0][1] - 10 <= y <= legend[0][1] + 160
    if legend:
        marks = [m for m in marks if not in_legend(m[0], m[1])]
    labels = []
    clip = spec.get("map_clip")
    for x0, y0, x1, y1, w, *_ in p.get_text("words"):
        if clip and not (clip[0] <= x0 <= clip[2] and clip[1] <= y0 <= clip[3]):
            continue
        if any(a <= x0 <= c and b <= y0 <= e for a, b, c, e in spec.get("map_exclude", ())):
            continue
        for n in re.findall(r"\d{1,3}", nfkc(w)):
            if in_legend(x0, y0):
                continue
            labels.append((int(n), (x0 + x1) / 2, (y0 + y1) / 2))
    return marks, labels


def leader_ends(doc, spec):
    """引き出し線（番号の箱から印へ伸びる直線）の両端。★栃木は番号の箱が印から離れていて、線でつながっている"""
    segs = []
    for d in doc[spec["map_page"]].get_drawings():
        for it in d["items"]:
            if it[0] == "l":
                segs.append((it[1].x, it[1].y, it[2].x, it[2].y))
    return segs


def pair(marks, labels, rows, segs=(), single_class=False):
    """番号 → 印。凡例どおりの区分の、最も近い印。★1つの印に複数の番号（「4,5,6」）があり得る。
    ★引き出し線があれば、番号から最も近い線の端をたどり、反対の端に最も近い印を採る"""
    cls_of = {int(r.no): ("*" if single_class else kind_class(r.kind)) for r in rows.itertuples()}
    out = {}
    for n, lx, ly in labels:
        c = cls_of.get(n)
        if c is None:
            continue
        if segs:
            best = None
            for x0, y0, x1, y1 in segs:
                for (ax, ay), (bx, by) in (((x0, y0), (x1, y1)), ((x1, y1), (x0, y0))):
                    d = math.hypot(ax - lx, ay - ly)
                    if d < 14 and (best is None or d < best[0]):
                        best = (d, bx, by)
            if best:
                lx, ly = best[1], best[2]
        cand = [(math.hypot(mx - lx, my - ly), mx, my) for mx, my, mc in marks if c == "*" or mc == c]
        if not cand:
            continue
        d, mx, my = min(cand)
        if n not in out or d < out[n][0]:
            out[n] = (d, mx, my)
    return out


# ── 3. 候補 ─────────────────────────────────────────────────────────
SUF = r"(トンネル|アンダーパス|アンダーボックス|アンダー|立体交差|立体|地下道|地下自動車道|架道橋|ガード|隧道|ずい道|陸橋|跨道橋)"


def load_osm(regions):
    fs = []
    for reg in regions:
        f = pd.read_parquet(ROOT / f"data/raw/final/{reg}.parquet", columns=["u", "v", "lon0", "lat0", "lon1", "lat1", "name", "ref", "state"])
        e = pd.read_parquet(ROOT / f"data/raw/roads/edges_{reg}.parquet", columns=["u", "v", "tunnel", "covered"])
        fs.append(f.merge(e.drop_duplicates(["u", "v"]), on=["u", "v"], how="left"))
    f = pd.concat(fs, ignore_index=True)
    f["lon"], f["lat"] = (f.lon0 + f.lon1) / 2, (f.lat0 + f.lat1) / 2
    f["struct"] = (f.state == "underpass") | f.tunnel.fillna("no").isin(["yes", "building_passage", "culvert"]) | (f.covered.fillna("no") == "yes")
    f["nm"] = f["name"].fillna("").map(nfkc)
    return f


def candidates(rows, f, pref):
    P = json.load(open(NAT / "web/public/data/places.json"))
    MU = P["munis"]
    k = 111320 * math.cos(math.radians(35.7))
    out = {}
    for r in rows.itertuples():
        munis = [x for x in re.split(r"[・,、]", nfkc(r.muni)) if x]
        mp = [(P["lon"][i] / 1e5, P["lat"][i] / 1e5, P["name"][i]) for i in range(len(P["name"]))
              if MU[P["m"][i]].startswith(pref) and any(m in MU[P["m"][i]] for m in munis)]
        if not mp:
            out[r.no] = []
            continue
        lo, la = np.array([q[0] for q in mp]), np.array([q[1] for q in mp])
        c = f[(f.lon > lo.min() - 0.01) & (f.lon < lo.max() + 0.01) & (f.lat > la.min() - 0.01) & (f.lat < la.max() + 0.01)]
        # ★列の境目は様式ごとにずれる（栃木は地先名が路線名の列に入る）。文字はまとめて見る
        place = nfkc(" ".join(str(x) for x in (r.kind, r.route, r.place) if isinstance(x, str)))
        cands = []
        for t in [x[0] for x in re.findall(r"([^\s()（）、]+?" + SUF + r")", place)]:
            core = re.sub(SUF + "$", "", t)
            if len(core) >= 2:
                h = c[c.nm.str.contains(re.escape(core))]
                cands += [(x, y, "名前") for x, y in zip(h.lon, h.lat)]
        # ★列の境目がずれて「道路種別」に路線名が入ることがある（東京 令和8年版）。両方を見る
        m = re.search(r"(\d+)号", place)
        if m and kind_class(r.kind) in ("国道", "都道"):
            onroute = c.ref.fillna("").str.split(";").apply(lambda xs: m.group(1) in xs)
            h = c[c.struct & onroute]
            cands += [(x, y, "路線番号の構造物") for x, y in zip(h.lon, h.lat)]
            # ★弱い候補: その路線の区間そのもの（構造物でない窪地・交差点付近の箇所のため）。
            #   地理参照の当てはめには使うが、**これだけで一致しても確定にしない**（線上はどこでも近くなるため）
            h = c[onroute & ~c.struct].iloc[::3]
            cands += [(x, y, "路線") for x, y in zip(h.lon, h.lat)]
        chome = [(x, y) for x, y, n in mp if len(n) >= 2 and nz(n) in nz(place)]
        if chome:
            cx, cy = np.mean([q[0] for q in chome]), np.mean([q[1] for q in chome])
            d = np.hypot((c.lon - cx) * k, (c.lat - cy) * 110570)
            h = c[c.struct & (d < 600)]
            cands += [(x, y, "町丁目の近くの構造物") for x, y in zip(h.lon, h.lat)]
        out[r.no] = cands
    return out


# ── 4. 地理参照 ─────────────────────────────────────────────────────
def georef(pairs, cands, seed_rows):
    k = 111320 * math.cos(math.radians(35.7))
    def fit(src, dst):
        A = np.c_[src, np.ones(len(src))]
        X, *_ = np.linalg.lstsq(A, dst, rcond=None)
        return X
    def pred(X, xy): return np.c_[np.atleast_2d(xy), np.ones(len(np.atleast_2d(xy)))] @ X
    def dist(a, b): return float(math.hypot((a[0] - b[0]) * k, (a[1] - b[1]) * 110570))
    src = [[pairs[n][1], pairs[n][2]] for n in seed_rows if n in pairs]
    dst = [np.mean([[x, y] for x, y, w in cands[n] if w == "名前"], axis=0) for n in seed_rows if n in pairs]
    if len(src) >= 4:
        X = fit(np.array(src), np.array(dst))
    else:
        # ★名前で当たる行が少ない県（茨城・栃木）: 印の分布と、行ごとの候補の重心の分布を、分位で粗く合わせる。
        #   地図は北が上・回転なし（縦横で別の縮尺の一次変換）とみなす。あとの反復で直す
        ns = [n for n in pairs if cands.get(n)]
        mx = np.array([pairs[n][1] for n in ns]); my = np.array([pairs[n][2] for n in ns])
        cx = np.array([np.median([c[0] for c in cands[n]]) for n in ns]); cy = np.array([np.median([c[1] for c in cands[n]]) for n in ns])
        q = [10, 90]
        ax = np.diff(np.percentile(cx, q))[0] / max(np.diff(np.percentile(mx, q))[0], 1e-9)
        ay = -np.diff(np.percentile(cy, q))[0] / max(np.diff(np.percentile(my, q))[0], 1e-9)
        bx = np.median(cx) - ax * np.median(mx); by = np.median(cy) - ay * np.median(my)
        X = np.array([[ax, 0.0], [0.0, ay], [bx, by]])
    for thr in (5000, 3000, 2000, 1200, 800, 800, 800):
        res = {}
        for n, (_, mx, my) in pairs.items():
            if not cands.get(n):
                continue
            p = pred(X, [mx, my])[0]
            # ★強い候補（構造物・名前）が 200m 以内にあればそれを採る。弱い候補（路線の区間）は線上どこでも近いので後回し
            strong = [c for c in cands[n] if c[2] != "路線"]
            bs = min(strong, key=lambda c: dist(p, c)) if strong else None
            best = bs if bs is not None and dist(p, bs) < 200 else min(cands[n], key=lambda c: dist(p, c))
            res[n] = (dist(p, best), best, p)
        inl = [n for n, v in res.items() if v[0] < thr]
        if len(inl) < 4:
            break
        X = fit(np.array([[pairs[n][1], pairs[n][2]] for n in inl]), np.array([res[n][1][:2] for n in inl]))
    # 帰無検査
    rng = np.random.default_rng(0)
    keys = list(res)
    null = []
    for _ in range(20):
        perm = rng.permutation(keys)
        ds = [min(dist(pred(X, [pairs[o][1], pairs[o][2]])[0], c) for c in cands[n]) for n, o in zip(keys, perm)]
        null.append(np.mean(np.array(ds) < 200))
    allpred = {n: pred(X, [mx, my])[0] for n, (_, mx, my) in pairs.items()}
    return res, allpred, float(np.mean(null))


def main(name: str) -> int:
    import pymupdf

    spec = SPECS[name]
    doc = pymupdf.open(PDF / f"{name}.pdf")
    rows = (parse_list_fixed(doc, spec["list_pages"], spec["list_cols"]) if spec.get("list_cols")
            else parse_list(doc, spec["list_pages"], spec.get("list_clip")))
    marks, labels = markers_and_labels(doc, spec)
    segs = leader_ends(doc, spec) if spec.get("leaders") else ()
    pairs = pair(marks, labels, rows, segs, single_class=len(set(spec["markers"].values())) == 1)
    f = load_osm(spec["regions"])
    cands = candidates(rows, f, spec["pref"])
    seeds = [n for n, c in cands.items() if any(w == "名前" for *_, w in c) and n in pairs]
    res, allpred, null = georef(pairs, cands, seeds)
    # ★地図の地理参照が十分に支えられているか（候補と 200m 以内で合う点が 8 以上）。
    #   支えられていれば、候補の無い行（構造物でない窪地・交差点付近など）も「印から推した位置」を円で出す
    good = sorted(v[0] for v in res.values() if v[0] < 200 and v[1][2] != "路線")
    anchored = len(good) >= 8
    # ★円の半径の下限 1km（2026-10-13 利用者判断。400m では利用者の知る箇所が円に入らなかった）。自主基準
    rad_u = int(max(1000, 2 * float(np.median(good)))) if good else 1000
    recs = []
    for r in rows.itertuples():
        n = r.no
        v = res.get(n)
        if v and v[0] < 200 and v[1][2] != "路線":
            tier, lon, lat, rad = "confirmed", v[1][0], v[1][1], ""
        elif anchored and n in allpred and (v is None or v[0] < 800 or not cands.get(n)):
            tier, lon, lat, rad = "uncertain", allpred[n][0], allpred[n][1], rad_u
        else:
            tier, lon, lat, rad = "unlocated", "", "", ""
        recs.append(dict(no=n, tier=tier, lon=lon, lat=lat, radius_m=rad, resid_m=round(v[0]) if v else "",
                         muni=r.muni, kind=r.kind, route=r.route, place=r.place))
    OUT.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(recs).to_csv(OUT / f"{name}.csv", index=False)
    from collections import Counter
    print(f"{name}: 一覧 {len(rows)} 行 / 印 {len(marks)} / 番号→印 {len(pairs)} / 候補あり {sum(1 for c in cands.values() if c)}"
          f" / 種 {len(seeds)} / 帰無検査 200m 以内 {null:.1%} / 地理参照の支え {len(good)} 点（{'可' if anchored else '不可'}）")
    print("  ", Counter(r["tier"] for r in recs))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
