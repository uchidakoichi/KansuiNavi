"""NK-09 全国展開: 各地方整備局の道路防災情報Webマップの冠水想定箇所（KML）を配布用の表に足す（2026-10-03）。

★位置は**整備局が公表している KML の座標**（推定ではない）。利用者判断 2026-10-03「Webマップの点データを使う」。
  各整備局のサイトは公共データ利用規約 第1.0版（PDL1.0）を適用（東北・北陸・中部・近畿・四国・九州・沖縄は局の頁で、
  中国は事務所の頁で確認。2026-10-03）。出典と、加工したことを書く。国が作成したかのように見せない。
★段の決め方（LLM は使わない）:
  confirmed … ① KML の点が、その点の PDF（text layer）に書かれた市区町村の中にある（N03 で判定）
              ② 書き出しで、橋でない道路の区間に 30m 以内で結び付く（→ export_national。結び付かなければ要確認へ落ちる）
  uncertain … ①が成り立たない・PDF が無い（座標は公表値なので円は小さく）
★試行（山梨・東京）の行は build_points.py が作る。この表はそれに**足す**（関東は Webマップの仕組みに無い）。

使い方（preprocess/ で）:
  uv run python tools/nk09_pilot/build_points.py
  uv run --with pymupdf python tools/nk09_webmap/build_points_webmap.py
"""

from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import locate_pdfs as LP  # noqa: E402

OUT = ROOT / "data/manual/road_kansui_points.csv"
BUREAU = {"北海道": "国土交通省 北海道開発局", "東北": "国土交通省 東北地方整備局", "北陸": "国土交通省 北陸地方整備局", "中部": "国土交通省 中部地方整備局",
          "近畿": "国土交通省 近畿地方整備局", "中国": "国土交通省 中国地方整備局", "四国": "国土交通省 四国地方整備局",
          "九州": "国土交通省 九州地方整備局", "沖縄": "内閣府 沖縄総合事務局"}
UNCERTAIN_R = 150     # 市区町村が合わない・PDF が無い点の円の半径［m］。★自主基準（座標は公表値）


def main() -> int:
    import os

    os.chdir(ROOT)
    from kansui.config import Config
    from build_places import municipalities
    import export_national as EN

    cfg = Config.load()
    kml = json.load(open(ROOT / "data/raw/nk09_webmap/kml_points.json"))
    inv = json.load(open(ROOT / "data/manual/nk09_webmap_inventory.json"))
    url_of = {Path(u).name: u for v in inv.values() for u in v.get("pdfs", [])}
    M, by_muni = LP.load_places()
    df = pd.DataFrame(kml)
    df["muni_at"], _ = municipalities(cfg, df)
    # 地域（書き出しで区間に結び付けるため）
    regions = list(cfg.require("national.regions"))
    import shapely
    from shapely.geometry import Point
    polys = {}
    for n in regions:
        polys[n] = EN.region_polygon(cfg, n)
        shapely.prepare(polys[n])
    def region_of(lon, lat):
        p = Point(lon, lat)
        return next((n for n, g in polys.items() if g.contains(p)), "")
    # ★★公表データ（KML）のリンク違い（2026-10-07）: 別々の場所の点が同じ PDF を指している（青森は 66 点に PDF 13 種）。
    #   → 共有されている PDF は、点の名前（住所）で一意に付け替えられたものだけ使う。付け替えられない点は
    #     要確認にし、説明表へのリンクを出さない（別の箇所の説明表を見せない）
    def pdfname(v): return v if isinstance(v, str) and v else None
    cnt = Counter(pdfname(v) for v in df["pdf"] if pdfname(v))
    shared = {k for k, n in cnt.items() if n > 1}
    all_pdfs = set(url_of)
    pool = all_pdfs - set(cnt)                 # どの点からもリンクされない PDF（付け替え先の候補）
    import pymupdf
    pool_txt = {}
    for f in pool:
        q = next(iter((ROOT / "data/cache/nk09_pdf").glob(f"*/{f}")), None)
        try:
            pool_txt[f] = LP.nz(pymupdf.open(q)[0].get_text()) if q else ""
        except Exception:
            pool_txt[f] = ""
    def name_key(n):
        k = LP.nz(n)
        k = re.sub(r"(地先|地内|付近|先|番地?|号)$", "", k)
        return re.sub(r"^(北海道|.{2,3}[都府県])", "", k)
    def pref_of(f): return re.sub(r"-02-\d+\.pdf$", "", f)
    relinked = {}
    for i, r in df.iterrows():
        pdf = pdfname(r["pdf"])
        if pdf in shared:
            k = name_key(r["name"])[:12]
            hits = [f for f in pool if pref_of(f) == pref_of(pdf) and len(k) >= 4 and k in pool_txt[f]]
            if len(hits) == 1:
                relinked[i] = hits[0]
                pool.discard(hits[0])
    rows = []
    for i, r in df.iterrows():
        pdf = pdfname(r["pdf"])
        broken = pdf in shared and i not in relinked
        if i in relinked:
            pdf = relinked[i]
        f = next(iter((ROOT / "data/cache/nk09_pdf").glob(f"*/{pdf}")), None) if pdf else None
        rec = LP.parse_one(f, M, by_muni) if f else {}
        munis = rec.get("muni", [])
        at = r["muni_at"] if isinstance(r["muni_at"], str) else ""
        # ★検査①: **座標が落ちる市区町村の名前が、その点の PDF の本文に書かれているか**（N03 で判定）。
        #   様式が整備局で違い「市町村」欄を確実には引けないので、欄の値ではなく本文全体で見る。
        #   政令市は区まで書かない PDF があるので、区を落とした市の名前でも可
        t = rec.get("_t", "")
        city = at.partition(" ")[2] if at else ""
        base = re.sub(r"(市).+区$", r"\1", city)
        same = bool(city) and (LP.nz(city) in t or (base != city and LP.nz(base) in t))
        # ★座標が丸い値（小数2桁以下）は仮置きとみなす（石川県-02-091 が 136.4, 36.35 だった）
        rough = all(len(f"{v:.6f}".rstrip("0").split(".")[1]) <= 2 for v in (r["lon"], r["lat"]))
        tier = "confirmed" if same and not rough and not broken else "uncertain"
        why = ("" if tier == "confirmed" else "公表データで説明表へのリンクが別の箇所と重複しているため、説明表は示していません" if broken
               else "座標が仮置きの値" if rough else "PDF が無い" if not f
               else "PDF の文字が読めない" if len(re.findall(r"[ぁ-んァ-ヶ一-龥]", t)) < 20
               else "PDF に座標の市区町村が書かれていない")
        route = rec.get("route") or ""
        fac = rec.get("facility") or r["name"] or ""
        rows.append(dict(
            id=(f"{Path(pdf).stem}-k{i}" if broken else Path(pdf).stem if pdf else f"kml-{r['bureau']}-{i}"),
            region=region_of(r["lon"], r["lat"]), tier=tier,
            lon=round(r["lon"], 6), lat=round(r["lat"], 6), radius_m="" if tier == "confirmed" else UNCERTAIN_R,
            route=" ".join(x for x in (rec.get("kind") or "", route) if x), place=" ".join(x for x in (at, rec.get("district") or "", fac) if x),
            manager=rec.get("id", "") and f"管理番号 {rec['id']}",
            src_title="道路防災情報Webマップ 冠水想定箇所 説明表", src_publisher=BUREAU.get(r["bureau"], r["bureau"]),
            src_date="2026年10月3日 取得", src_url="" if broken else url_of.get(pdf, ""), check_page=1,
            check_hint=(why if broken else "説明表の「位置図」" + (f"（{why}）" if why else "")),
            method="整備局の道路防災情報Webマップの位置（KML）"))
    # ★B1（2026-10-07 利用者判断）: どの点からもリンクされない PDF ＝座標が公表されていない箇所。
    #   位置を推定せず「位置不明」として一覧に出す（市区町村・地先名・説明表へのリンク）
    linked = {Path(r["src_url"]).name for r in rows if r["src_url"]}
    for f in sorted(all_pdfs - linked):
        q = next(iter((ROOT / "data/cache/nk09_pdf").glob(f"*/{f}")), None)
        rec = LP.parse_one(q, M, by_muni) if q else {}
        bureau = next((b for b, v in inv.items() if any(u.endswith("/" + f) for u in v.get("pdfs", []))), "")
        rows.append(dict(
            id=Path(f).stem, region="", tier="unlocated", lon="", lat="", radius_m="",
            route=" ".join(x for x in (rec.get("kind") or "", rec.get("route") or "") if x),
            place=" ".join(x for x in ((rec.get("muni") or [""])[0], rec.get("district") or "", rec.get("facility") or "") if x)
                  or (rec.get("id") or Path(f).stem),
            manager=rec.get("id", "") and f"管理番号 {rec['id']}",
            src_title="道路防災情報Webマップ 冠水想定箇所 説明表", src_publisher=BUREAU.get(bureau, bureau),
            src_date="2026年10月3日 取得", src_url=url_of[f], check_page=1,
            check_hint="説明表の「位置図」（座標が公表されていない箇所）",
            method="整備局の道路防災情報Webマップ（説明表のみ・座標の公表なし）"))
    print("付け替えた点", len(relinked), "／ リンク違いで説明表を出さない点", sum(1 for r in rows if "重複" in r["check_hint"]))
    # 試行の行（build_points.py の出力）の後ろに足す。★同じ id は足さない
    old = list(csv.DictReader(open(OUT, encoding="utf-8")))
    old = [o for o in old if not o["method"].startswith("整備局の道路防災情報Webマップ")]
    have = {o["id"] for o in old}
    rows = [r for r in rows if r["id"] not in have]
    with open(OUT, "w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=list(old[0].keys()))
        w.writeheader()
        w.writerows(old)
        w.writerows(rows)
    print("Webマップの点", len(rows), Counter(r["tier"] for r in rows), "地域なし", sum(1 for r in rows if not r["region"]))
    print("要確認の理由", Counter(r["check_hint"] for r in rows if r["tier"] != "confirmed"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
