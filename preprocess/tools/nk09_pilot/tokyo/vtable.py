"""Vision OCR の行を表に組む（位置だけで決める）。行の目印は都道府県名の列の「東京都」。"""
import json
COLS = [("muni", 0.255, 0.33), ("kind", 0.33, 0.385), ("route", 0.385, 0.49), ("place", 0.49, 0.83)]
out = {}; start = {"ocr4.txt": 1, "ocr5.txt": 71}
for fn, first in start.items():
    items = []
    for l in open(fn):
        b, t = l.rstrip("\n").split("\t", 1); x, y, w, h = map(float, b.split()); items.append((x, y + h / 2, t))
    anchors = sorted(y for x, y, t in items if 0.20 <= x < 0.255 and t == "東京都")
    print(fn, "行数", len(anchors))
    for i, ay in enumerate(anchors):
        d = {}
        for x, y, t in items:
            if abs(y - ay) > 0.005: continue
            c = next((c for c, a, b in COLS if a <= x < b), None)
            if c: d.setdefault(c, []).append((x, t))
        out[first + i] = {c: "".join(t for _, t in sorted(v)) for c, v in d.items()}
json.dump(out, open("vision_table.json", "w"), ensure_ascii=False, indent=0)
for n in (1, 9, 26, 57, 66, 70, 71, 100, 135): print(n, out.get(n))
