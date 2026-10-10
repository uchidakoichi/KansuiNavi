"""NK-09 全国展開: 道路防災情報Webマップの「冠水想定箇所 説明表」PDF（1箇所1枚）から位置を起こす（2026-10-03）。

★LLM を使わない。各段を機械で検査する（利用者の要求「LLM が間違えるなら人が見ても信用できない」）。
  1 文字   … PDF の text layer。都道府県は管理番号から、市区町村・字丁目は places.json の地名との一致で決める
             （様式が整備局で違うので、ラベルの位置には頼らない）
  2 印     … 位置図の中の**純粋な赤**の画素（矢印①②・強調した区間）の中央値。広がり＝不確かさ
  3 位置   … 位置図を地理院タイル（標準地図 z16）に画像照合。探す範囲は字丁目（無ければ市区町村）の代表点の周り
  4 検査   … 照合の一致度（1位と2位の差）・字丁目からの距離・赤の広がり → 段（confirmed / uncertain / unlocated）
★段の基準は config ではなく下の定数（試行用）。決めたら config.yaml へ移す。

使い方（preprocess/ で）:
  uv run --with opencv-python-headless --with pymupdf python tools/nk09_webmap/locate_pdfs.py parse
  uv run ... locate_pdfs.py tiles      # 地理院タイルを逐次で取る（TileFetcher: 逐次・キャッシュ・UA）
  uv run ... locate_pdfs.py match      # 画像照合（手元の計算だけ。CPU で並列）
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import sys
import unicodedata
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]          # preprocess/
PDF_DIR = ROOT / "data/cache/nk09_pdf"
WORK = ROOT / "data/raw/nk09_webmap"
PLACES = ROOT.parent / "web/public/data/places.json"
INV = Path(os.environ.get("NK09_INVENTORY", ROOT / "data/manual/nk09_webmap_inventory.json"))
Z = 16
RENDER = 3.0                     # 位置図の描画倍率
R_DISTRICT_M = 1600              # 字丁目が分かったときの探索半径
R_MUNI_M = 3200                  # 市区町村しか分からないとき
URL = "https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png"
# 段の基準（試行・自主基準）
SCORE_MIN, MARGIN_MIN = 0.35, 0.15
SPREAD_CONFIRMED_M = 80          # 赤の広がりがこれ以下なら点、超えたら円
DISTRICT_MAX_M = 2500            # 照合した位置が字丁目の代表点からこれ以上離れたら外れ

KAN = str.maketrans("一二三四五六七八九", "123456789")
PREFS = ("北海道 青森県 岩手県 宮城県 秋田県 山形県 福島県 茨城県 栃木県 群馬県 埼玉県 千葉県 東京都 神奈川県 新潟県 富山県 "
         "石川県 福井県 山梨県 長野県 岐阜県 静岡県 愛知県 三重県 滋賀県 京都府 大阪府 兵庫県 奈良県 和歌山県 鳥取県 島根県 "
         "岡山県 広島県 山口県 徳島県 香川県 愛媛県 高知県 福岡県 佐賀県 長崎県 熊本県 大分県 宮崎県 鹿児島県 沖縄県").split()


#: 異体字（塩竈/塩釜 など）。★比べるときだけ寄せる
VAR = str.maketrans({"竈": "釜", "﨑": "崎", "嵜": "崎", "龍": "竜", "檜": "桧", "櫻": "桜", "澤": "沢", "邊": "辺", "邉": "辺",
                     "齋": "斎", "齊": "斉", "濱": "浜", "髙": "高", "槇": "槙", "冨": "富", "嶋": "島", "曽": "曾", "ヶ": "ケ"})


def nz(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s", "", s).replace("丁目", "").translate(KAN).translate(VAR)


# ── 1. 文字 ─────────────────────────────────────────────────────────
def load_places():
    P = json.load(open(PLACES))
    M = P["munis"]
    by_muni: dict[int, list] = {}
    for n, m, lo, la in zip(P["name"], P["m"], P["lon"], P["lat"]):
        by_muni.setdefault(m, []).append((nz(n), n, lo / 1e5, la / 1e5))
    return M, by_muni


def parse_one(fn: Path, M, by_muni) -> dict:
    import pymupdf

    d = pymupdf.open(fn)
    p = d[0]
    raw = p.get_text()
    t = nz(raw)
    rec: dict = {"file": str(fn.relative_to(PDF_DIR)), "pages": d.page_count, "_t": t}
    m = re.search("(" + "|".join(PREFS) + r")-?02-?(\d+)", t)
    rec["id"] = f"{m.group(1)}-02-{int(m.group(2)):03d}" if m else fn.stem
    pref = m.group(1) if m else None
    # 緯度経度が書いてある様式（中国の一部）
    ll = re.findall(r"(\d{2,3}\.\d{4,})", t)
    lats = [float(x) for x in ll if 24 < float(x) < 46]
    lons = [float(x) for x in ll if 122 < float(x) < 154]
    if lats and lons:
        rec["text_lonlat"] = [lons[0], lats[0]]
    # 市区町村: その都道府県の市区町村名のうち、本文に現れる最も長いもの
    cands = []
    for i, ms in enumerate(M):
        pf, _, city = ms.partition(" ")
        if pref and pf != pref:
            continue
        c = nz(city)
        if c and c in t:
            cands.append((len(c), i))
        else:
            # 政令市の区: 「札幌市中央区」が「札幌市」とだけ書かれることがある
            base = re.sub(r"(市).+区$", r"\1", c)
            if base != c and base in t:
                cands.append((len(base) - 0.5, i))
    munis = [i for L, i in sorted(cands, reverse=True) if L == sorted(cands, reverse=True)[0][0]] if cands else []
    rec["muni"] = [M[i] for i in munis]
    # 字丁目: その市区町村の地名のうち、本文に現れる最も長いもの
    best = None
    for i in munis:
        for key, name, lo, la in by_muni.get(i, []):
            if len(key) >= 2 and key in t and (best is None or len(key) > len(best[0])):
                best = (key, name, lo, la)
    if best:
        rec["district"] = best[1]
        rec["center"] = [best[2], best[3]]
        rec["radius_m"] = R_DISTRICT_M
    elif munis:
        pts = [(lo, la) for i in munis for _, _, lo, la in by_muni.get(i, [])]
        if pts:
            rec["center"] = [float(np.median([q[0] for q in pts])), float(np.median([q[1] for q in pts]))]
            rec["radius_m"] = R_MUNI_M
    # 表示用（あれば）。ラベルの直後の行
    # ★表の様式なので、値はラベルの**下のセル**にある（本文の順では離れている）。位置で引く
    spans = []
    for b in p.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            txt = "".join(sp["text"] for sp in ln["spans"]).strip()
            if txt:
                spans.append((ln["bbox"], txt))
    def after(*labels):
        for lab in labels:
            for (x0, y0, x1, y1), tx in spans:
                if re.sub(r"\s", "", tx) != lab:
                    continue
                cx = (x0 + x1) / 2
                below = [(by0 - y1, t2) for (bx0, by0, bx1, by1), t2 in spans
                         if by0 > y1 - 1 and bx0 - 30 < cx < bx1 + 30 and by0 - y1 < 60
                         and re.sub(r"\s", "", t2) != lab]
                if below:
                    return min(below)[1]
        return ""
    # ★市区町村は**「市町村」欄の値を優先**する（本文全体の最長一致は警察署・消防署の名前を拾う。2026-10-03）
    field = nz(after("市町村", "市区町村"))
    if field:
        hit = [i for i, ms in enumerate(M) if (not pref or ms.startswith(pref + " "))
               and (nz(ms.partition(" ")[2]) == field or nz(ms.partition(" ")[2]).startswith(field)
                    or field.endswith(nz(ms.partition(" ")[2])))]
        if hit:
            munis = hit
            rec["muni"] = [M[i] for i in munis]
            rec["muni_from"] = "欄"
    rec["route"] = after("箇所名称", "道路名", "路線名")
    rec["facility"] = after("アンダーパス等名称", "施設名", "名称（通称）")
    rec["kind"] = after("種別")
    # 位置図＝ページ内で最大の画像。★赤い印の画素を数える
    infos = p.get_image_info()
    if not infos:
        rec["err"] = "画像が無い"
        return rec
    big = max(infos, key=lambda i: (i["bbox"][2] - i["bbox"][0]) * (i["bbox"][3] - i["bbox"][1]))
    rec["map_bbox"] = list(big["bbox"])
    return rec


def cmd_parse():
    from concurrent.futures import ProcessPoolExecutor

    M, by_muni = load_places()
    files = sorted(PDF_DIR.rglob("*.pdf"))
    WORK.mkdir(parents=True, exist_ok=True)
    recs = [parse_one(f, M, by_muni) for f in files]
    json.dump(recs, open(WORK / "parsed.json", "w"), ensure_ascii=False)
    n = len(recs)
    print(f"PDF {n} / 市区町村が決まった {sum(1 for r in recs if r.get('muni'))} / 字丁目まで {sum(1 for r in recs if r.get('district'))}"
          f" / 緯度経度の記載 {sum(1 for r in recs if r.get('text_lonlat'))} / 画像なし {sum(1 for r in recs if r.get('err'))}")


# ── 2. タイル ────────────────────────────────────────────────────────
def tile_xy(lon, lat, z=Z):
    n = 2 ** z
    return (lon + 180) / 360 * n, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n


def to_ll(fx, fy, z=Z):
    n = 2 ** z
    return fx / n * 360 - 180, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * fy / n))))


def tile_box(rec):
    lon, lat = rec["center"]
    fx, fy = tile_xy(lon, lat)
    span = rec["radius_m"] / (40075016 * math.cos(math.radians(lat)) / 2 ** Z)
    return int(fx - span), int(fx + span), int(fy - span), int(fy + span)


def cmd_tiles():
    sys.path.insert(0, str(ROOT / "src"))
    os.chdir(ROOT)
    from kansui.config import Config
    from kansui.fetcher import TileFetcher

    F = TileFetcher.from_config(Config.load())
    recs = json.load(open(WORK / "parsed.json"))
    need = set()
    for r in recs:
        if "center" in r:
            x0, x1, y0, y1 = tile_box(r)
            need |= {(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)}
    print("タイル", len(need), flush=True)
    for i, (x, y) in enumerate(sorted(need), 1):
        if F.fetch(URL.format(z=Z, x=x, y=y)) is None:
            raise SystemExit("★通信の失敗")
        if i % 2000 == 0:
            print(i, flush=True)
    print("完了", flush=True)


# ── 3. 照合 ─────────────────────────────────────────────────────────
def _match(rec):
    import cv2
    import pymupdf
    from PIL import Image

    sys.path.insert(0, str(ROOT / "src"))
    os.chdir(ROOT)
    from kansui.config import Config
    from kansui.fetcher import TileFetcher, NOT_FOUND

    out = {"id": rec["id"], "file": rec["file"]}
    if "center" not in rec or "map_bbox" not in rec:
        out["tier"], out["why"] = "unlocated", "市区町村または位置図が無い"
        return out
    F = TileFetcher.from_config(Config.load())
    d = pymupdf.open(PDF_DIR / rec["file"])
    p = d[0]
    pix = p.get_pixmap(matrix=pymupdf.Matrix(RENDER, RENDER), clip=pymupdf.Rect(*rec["map_bbox"]))
    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3].copy()
    R, G, B = (a[..., i].astype(int) for i in range(3))
    red = (R > 200) & (G < 70) & (B < 70)
    if red.sum() < 15:
        out["tier"], out["why"] = "unlocated", "位置図に赤い印が無い"
        return out
    ys, xs = np.nonzero(red)
    cx, cy = float(np.median(xs)), float(np.median(ys))
    spread_px = float(np.percentile(np.hypot(xs - cx, ys - cy), 80))
    # タイルの寄せ集め
    x0, x1, y0, y1 = tile_box(rec)
    mos = np.full(((y1 - y0 + 1) * 256, (x1 - x0 + 1) * 256, 3), 255, np.uint8)
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            b = F.fetch(URL.format(z=Z, x=x, y=y))
            if isinstance(b, bytes):
                mos[(y - y0) * 256:(y - y0 + 1) * 256, (x - x0) * 256:(x - x0 + 1) * 256] = \
                    np.array(Image.open(io.BytesIO(b)).convert("RGB"))
    # 赤い印は照合の邪魔なので白で塗る
    tpl = a.copy()
    tpl[red] = 255
    k = 420 / max(tpl.shape[:2])
    tpl = cv2.resize(tpl, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    cxs, cys = cx * k, cy * k
    g = lambda im: cv2.GaussianBlur(cv2.Canny(cv2.cvtColor(im, cv2.COLOR_RGB2GRAY), 60, 160).astype(np.float32), (0, 0), 1.5)
    gm = g(mos)
    best = (-1.0,)
    for s in np.geomspace(0.12, 2.4, 30):
        t = cv2.resize(tpl, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        if min(t.shape[:2]) < 40 or t.shape[0] >= gm.shape[0] or t.shape[1] >= gm.shape[1]:
            continue
        gt = g(t)
        r = cv2.matchTemplate(gm, gt, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(r)
        if mx > best[0]:
            rr = r.copy()
            h, w = gt.shape
            rr[max(0, loc[1] - h // 3):loc[1] + h // 3, max(0, loc[0] - w // 3):loc[0] + w // 3] = -1
            best = (float(mx), float(s), loc, float(rr.max()))
    if best[0] < 0:
        out["tier"], out["why"] = "unlocated", "照合できない"
        return out
    mx, s, loc, second = best
    px, py = loc[0] + cxs * s, loc[1] + cys * s
    lon, lat = to_ll(x0 + px / 256, y0 + py / 256)
    m_per_px = 40075016 * math.cos(math.radians(lat)) / 2 ** Z / 256 / (s * k)   # 位置図の元の描画画素あたり
    spread_m = spread_px * m_per_px
    dlon, dlat = lon - rec["center"][0], lat - rec["center"][1]
    dist_c = math.hypot(dlon * 111320 * math.cos(math.radians(lat)), dlat * 110570)
    out.update(lon=round(lon, 6), lat=round(lat, 6), score=round(mx, 3), second=round(second, 3),
               scale=round(s, 3), spread_m=round(spread_m), dist_center_m=round(dist_c))
    if mx < SCORE_MIN or mx - second < MARGIN_MIN:
        out["tier"], out["why"] = "unlocated", "照合の一致度が低い"
    elif rec.get("district") and dist_c > DISTRICT_MAX_M:
        out["tier"], out["why"] = "unlocated", "字丁目から遠い"
    elif spread_m <= SPREAD_CONFIRMED_M:
        out["tier"] = "confirmed"
    else:
        out["tier"], out["radius_m"] = "uncertain", int(min(max(spread_m, 150), 800))
    return out


def cmd_match():
    from concurrent.futures import ProcessPoolExecutor

    recs = json.load(open(WORK / "parsed.json"))
    only = os.environ.get("NK09_ONLY")
    if only:
        recs = [r for r in recs if r["file"].startswith(only)]
    with ProcessPoolExecutor(max_workers=max(2, (os.cpu_count() or 4) - 2)) as ex:
        res = list(ex.map(_match, recs, chunksize=4))
    json.dump(res, open(WORK / f"matched{'_' + only if only else ''}.json", "w"), ensure_ascii=False)
    from collections import Counter
    print(Counter(r["tier"] for r in res), Counter(r.get("why") for r in res if r.get("why")))


if __name__ == "__main__":
    {"parse": cmd_parse, "tiles": cmd_tiles, "match": cmd_match}[sys.argv[1]]()
