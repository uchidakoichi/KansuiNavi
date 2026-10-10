"""NK-09: 取得した各整備局の冠水想定箇所 KML（data/cache/nk09_kml/{局}/*.kml）を点の表にする。

出力: data/raw/nk09_webmap/kml_points.json（bureau, kml, lon, lat, pdf, name）
★KML は道路防災情報Webマップが読み込むもの（公表データ・PDL1.0）。取得は 2026-10-03、逐次・1秒待ち・UA 明示。
"""
import glob
import json
import os
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
pts = []
for fn in sorted(glob.glob(str(ROOT / "data/cache/nk09_kml/*/*.kml"))):
    s = open(fn, "rb").read().decode("utf-8", "ignore")
    for pm in re.findall(r"<Placemark.*?</Placemark>", s, re.S):
        c = re.search(r"<coordinates>\s*([-\d.]+),([-\d.]+)", pm)
        if not c:
            continue
        pdf = re.search(r"([a-z]+-02-\d+\.pdf)", pm)
        name = re.search(r"<name>(.*?)</name>", pm, re.S)
        pts.append(dict(bureau=Path(fn).parent.name, kml=os.path.basename(fn), lon=float(c.group(1)), lat=float(c.group(2)),
                        pdf=pdf.group(1) if pdf else None, name=name.group(1).strip() if name else ""))
out = ROOT / "data/raw/nk09_webmap/kml_points.json"
out.parent.mkdir(parents=True, exist_ok=True)
json.dump(pts, open(out, "w"), ensure_ascii=False)
print("点", len(pts), Counter(p["bureau"] for p in pts))
