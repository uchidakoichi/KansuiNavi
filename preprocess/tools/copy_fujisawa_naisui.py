"""藤沢市の内水（雨水出水）浸水想定区域を、藤沢版の配布物から全国版の配布データへ写す（2026-10-11）。

★ハザードマップポータルの内水の配信に藤沢市は無い（naisui.csv 2026-09-17）。藤沢版は PLATEAU（PDL1.0）の
  `udx/ifld/`（「藤沢市雨水出水浸水想定区域図」）から面を作っていた（藤沢版 kansui.naisui_overlay・T-03）。
  全国版の前処理には PLATEAU の取り込みが無いので、**藤沢版の出力をそのまま使う**。
★出力は配布データ（git に入れない）。出したら web/tools/deploy-data.sh でデータのプロジェクトへ。

使い方（preprocess/ で）: uv run python tools/copy_fujisawa_naisui.py [../../Kansui/web/public/data/naisui.json]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / "Kansui/web/public/data/naisui.json"
OUT = ROOT / "web/public/data/naisui_fujisawa.json"

d = json.loads(SRC.read_text(encoding="utf-8"))
# ★中身で確かめる（出典と、色分けに使う属性が揃っていること）
assert d.get("type") == "FeatureCollection" and "PLATEAU" in d.get("attribution", ""), "藤沢版の naisui.json ではない"
assert all({"color", "label", "lo", "hi"} <= f["properties"].keys() for f in d["features"]), "色分けの属性が無い"
OUT.write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print(f"{OUT}  {OUT.stat().st_size / 1e6:.1f}MB  帯 {len(d['features'])}")
