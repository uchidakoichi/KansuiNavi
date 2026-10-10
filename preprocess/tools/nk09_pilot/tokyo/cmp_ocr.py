import json, unicodedata, re
V = json.load(open("vision_table.json"))
def n(s): 
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"[\s・•･·()（）〜～\-－ー−]", "", s).replace("&", "")
agree = {"muni": 0, "kindroute": 0, "place": 0}; diffs = []
for l in open("llm_rows.tsv"):
    no, muni, kind, route, place = l.rstrip("\n").split("\t"); v = V[no]
    pairs = {"muni": (muni, v.get("muni", "")), "kindroute": (kind + route, v.get("kind", "") + v.get("route", "")), "place": (place, v.get("place", ""))}
    for k, (a, b) in pairs.items():
        if n(a) == n(b): agree[k] += 1
        else: diffs.append((int(no), k, a, b))
print("完全一致（正規化後）:", {k: f"{c}/135" for k, c in agree.items()})
rows_all = 135 - len({d[0] for d in diffs}); print(f"3項目すべて一致した行: {rows_all}/135")
for d in diffs: print(d)
