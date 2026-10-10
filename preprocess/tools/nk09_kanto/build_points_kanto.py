"""NK-09 関東: overview.py の結果（data/raw/nk09_kanto/{都県}.csv）を配布用の表に足す（2026-10-06）。

★関東の位置は**本アプリの推定**（全域図の番号の印を地理参照し、OSM の立体交差・トンネルと照合）。座標は公表されていない。
★東京は令和8年6月15日版で、試行に使った平成29年版（tools/nk09_pilot/tokyo）を**置き換える**。
★山梨の試行（令和6年8月1日・詳細図の画像照合）は残す（関東地整の令和8年版の山梨は画像だけで、番号が読めない）。

使い方（preprocess/ で。build_points.py → build_points_webmap.py の後）:
  uv run --with pymupdf python tools/nk09_kanto/build_points_kanto.py tokyo ibaraki tochigi kanagawa
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import overview as O  # noqa: E402

OUT = ROOT / "data/manual/road_kansui_points.csv"
METHOD = "関東地方整備局の全域図の番号の印を地理参照し、OSM の立体交差・トンネルと照合（本アプリの推定）"


def main(names: list[str]) -> int:
    old = list(csv.DictReader(open(OUT, encoding="utf-8")))
    fields = list(old[0].keys())
    drop_ids = {f"tokyo-{i}" for i in range(1, 200)} if "tokyo" in names else set()
    keep = [o for o in old if o["id"] not in drop_ids and not o["id"].startswith(tuple(f"kanto-{n}-" for n in names))]
    add = []
    for n in names:
        spec = O.SPECS[n]
        df = pd.read_csv(ROOT / f"data/raw/nk09_kanto/{n}.csv", dtype=str, keep_default_na=False)
        for r in df.itertuples():
            page = spec["map_page"] + 1
            add.append(dict(
                id=f"kanto-{n}-{r.no}", region=spec["regions"][0], tier=r.tier, lon=r.lon, lat=r.lat,
                radius_m=r.radius_m, route=" ".join(x for x in (r.kind, r.route) if x).strip(),
                place=" ".join(x for x in (spec["pref"], r.muni, r.place) if x).strip(), manager="",
                src_title=spec["title"], src_publisher=spec["publisher"], src_date=spec["date"], src_url=spec["url"],
                check_page=page, check_hint=f"{page}ページの地図の番号 {r.no} の印（一覧の No.{r.no}）", method=METHOD))
    with open(OUT, "w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=fields)
        w.writeheader()
        w.writerows(keep)
        w.writerows(add)
    print("関東", len(add), Counter((a["id"].split("-")[1], a["tier"]) for a in add), "置き換えた試行の行", len(old) - len(keep))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
