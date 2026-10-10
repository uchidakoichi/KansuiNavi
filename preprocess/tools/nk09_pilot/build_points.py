"""NK-09 試行（山梨・東京）の結果を、配布用の1つの表にまとめる（data/manual/road_kansui_points.csv）。

段（tier）— 2026-10-02 利用者判断:
  confirmed  … 2つの独立な手がかりが一致。経路計算に効かせる（その区間を最上位のリスクに）
  uncertain  … おおよその位置だけ。円で表示し、経路計算には効かせない。目視の確認方法を案内する
  unlocated  … 位置を決められない。地図には出さず、資料の一覧として出す
"""
import csv, json, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from rows import ROWS
OUT = HERE.parents[1] / "data/manual/road_kansui_points.csv"
recs = []
# ── 山梨（詳細図の画像照合） ──
Y = {"title": "山梨県内の道路冠水注意箇所（詳細図）", "publisher": "国土交通省 関東地方整備局 甲府河川国道事務所",
     "date": "令和6年8月1日時点", "url": "https://www.ktr.mlit.go.jp/ktr_content/content/000889858.pdf"}
reg = {r["no"]: r for r in json.load(open(HERE / "register_out.json"))}
for r in json.load(open(HERE / "register_retry.json")): reg[r["no"]] = r
for no, mgr, route, place, alias, *_ in ROWS:
    g = reg[no]; ok = g["score"] >= 0.4 and g["score"] - g["second"] >= 0.2
    recs.append(dict(id=f"yamanashi-{no}", region="yamanashi", tier="confirmed" if ok else "unlocated",
                     lon=round(g["lon"], 6) if ok else "", lat=round(g["lat"], 6) if ok else "", radius_m="",
                     route=route, place=f"{place}（{alias}）", manager=mgr, src_title=Y["title"], src_publisher=Y["publisher"],
                     src_date=Y["date"], src_url=Y["url"], check_page=no,
                     check_hint=f"{no}ページの詳細図の赤丸", method="詳細図の画像を地理院地図に照合"))
# ── 東京（全域図の印＋OSM の構造物） ──
TK = {"publisher": "国土交通省 関東地方整備局 東京国道事務所・相武国道事務所（記者発表資料）", "date": "平成29年8月4日作成",
      "url": "https://www.ktr.mlit.go.jp/ktr_content/content/000725521.pdf",
      "title": "アンダーパス部の道路冠水注意箇所マップ・リスト【東京都】"}
G = json.load(open(HERE / "tokyo/georef_out.json"))
for l in open(HERE / "tokyo/llm_rows.tsv"):
    no, muni, kind, route, place = l.rstrip("\n").split("\t"); n = int(no); g = G.get(no, {})
    res = g.get("resid_m")
    mgr = ("国土交通省 東京国道事務所" if n <= 18 else "国土交通省 相武国道事務所" if n <= 24 else
           "東京都" if kind == "都道" else muni)
    if res is not None and res < 200:
        tier, lon, lat, rad = "confirmed", g["lon"], g["lat"], ""
    elif res is not None and res < 800 and "map_lon" in g:
        tier, lon, lat, rad = "uncertain", g["map_lon"], g["map_lat"], 400
    else:
        tier, lon, lat, rad = "unlocated", "", "", ""
    recs.append(dict(id=f"tokyo-{n}", region="tokyo_kanagawa", tier=tier,
                     lon=round(lon, 6) if lon != "" else "", lat=round(lat, 6) if lat != "" else "", radius_m=rad,
                     route=f"{kind} {route}", place=f"{muni} {place}", manager=mgr, src_title=TK["title"],
                     src_publisher=TK["publisher"], src_date=TK["date"], src_url=TK["url"], check_page=2,
                     check_hint=f"2ページの地図の番号 {n} の印（一覧は{4 if n <= 70 else 5}ページの No.{n}）",
                     method="全域図の番号の印と OSM の立体交差・トンネルを照合"))
OUT.parent.mkdir(parents=True, exist_ok=True)
with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(recs[0])); w.writeheader(); w.writerows(recs)
from collections import Counter
print(OUT, Counter((r["region"], r["tier"]) for r in recs))
