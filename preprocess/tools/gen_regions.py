"""都道府県ごとの地域定義（config.yaml の `regions:`）を N03 から組み立てて表示する。

    uv run python tools/gen_regions.py 01 02 03 ... > /tmp/regions.yaml

★**config.yaml を書き換えない。** 出力を人が読んで貼る（地域の定義は決定事項なので）。

決めること:
  1. ★平面直角座標系が1つに決まらない県は、**系ごとに市区町村で分ける**（NK-08）。
     Δh・アンダーパス・幅員の照合はメートルで測るので、1地域＝1系でなければならない。
  2. どの Geofabrik 地方の PBF を使うか。★地方の範囲ポリゴン（`.poly`）に
     市区町村の代表点が**全部**入る地方を選ぶ。1つに決まらなければ落とす。
  3. ★所属未定地（`xx000`）のうち、他の区域から 3km より離れたものを外す
     （東京都の 13000 が北緯 29.79度まで範囲を伸ばした件。2026-09-23）。
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from kansui import boundary as B
from kansui.config import Config
from kansui.fetcher import TileFetcher
from kansui.region import N03_CODE, N03_PREF, Region, places, zones

POLY = Path("data/external/poly")

#: JIS X 0401 の都道府県コード → 地域名に使うローマ字（★名前の表であって判定には使わない）
ROMAJI = {
    "01": "hokkaido", "02": "aomori", "03": "iwate", "04": "miyagi", "05": "akita",
    "06": "yamagata", "07": "fukushima", "08": "ibaraki", "09": "tochigi", "10": "gunma",
    "11": "saitama", "12": "chiba", "13": "tokyo", "14": "kanagawa", "15": "niigata",
    "16": "toyama", "17": "ishikawa", "18": "fukui", "19": "yamanashi", "20": "nagano",
    "21": "gifu", "22": "shizuoka", "23": "aichi", "24": "mie", "25": "shiga",
    "26": "kyoto", "27": "osaka", "28": "hyogo", "29": "nara", "30": "wakayama",
    "31": "tottori", "32": "shimane", "33": "okayama", "34": "hiroshima", "35": "yamaguchi",
    "36": "tokushima", "37": "kagawa", "38": "ehime", "39": "kochi", "40": "fukuoka",
    "41": "saga", "42": "nagasaki", "43": "kumamoto", "44": "oita", "45": "miyazaki",
    "46": "kagoshima", "47": "okinawa",
}


def read_poly(p: Path) -> Polygon:
    """Osmosis の .poly（1つ以上の輪。`!` で始まる輪は穴）。"""
    rings, holes, cur, hole = [], [], None, False
    for line in p.read_text().splitlines()[1:]:
        s = line.strip()
        if not s:
            continue
        if s == "END":
            if cur is None:
                break
            (holes if hole else rings).append(cur)
            cur = None
            continue
        if cur is None:
            hole = s.startswith("!")
            cur = []
            continue
        x, y = map(float, s.split()[:2])
        cur.append((x, y))
    return unary_union([Polygon(r) for r in rings]).difference(
        unary_union([Polygon(h) for h in holes]) if holes else Polygon())


def main() -> int:
    codes = sys.argv[1:]
    cfg = Config.load()
    fetcher = TileFetcher.from_config(cfg)
    extracts = {p.stem: read_poly(p) for p in sorted(POLY.glob("*.poly"))}
    for pref in codes:
        name = ROMAJI[pref]
        g = B.load(fetcher, cfg.require("boundary.url_template"),
                   Region(name=name, pref_codes=(pref,)),
                   cfg.resolve_path("boundary.cache_dir")).to_crs(4326)
        label = str(g[N03_PREF].iloc[0])
        code = g[N03_CODE].astype(str)

        # --- 所属未定地（xx000）: 他から 3km より離れたものは外す ---------------
        exclude = []
        unassigned = sorted(c for c in set(code) if c.endswith("000"))
        if unassigned:
            # ★距離はその県の中心の正距方位図法で測る（系が決まる前なので、系の座標は使えない）
            c0 = g.union_all().centroid
            aeqd = f"+proj=aeqd +lat_0={c0.y} +lon_0={c0.x} +units=m +ellps=GRS80"
            rest = g[~code.isin(unassigned)].to_crs(aeqd)
            rest_u = rest.union_all().buffer(3000)
            for c in unassigned:
                part = g[code == c].to_crs(rest.crs)
                if not part.geometry.within(rest_u).all():
                    exclude.append(c)

        # --- PBF の地方: 代表点の過半数が入る地方 ----------------------------
        #   ★全体が地方の範囲の外にある市区町村は外す（OSM の抽出に道路が無い）。
        #     北海道の 01695〜01700（北方領土の6村）がこれに当たる（2026-09-23 に実測）。
        #   ★一部だけ外にある市区町村は残して報告する（根室市の歯舞群島の部分など）。
        pl = places(g)
        votes: dict[str, int] = defaultdict(int)
        for p in pl:
            for k, poly in extracts.items():
                if poly.contains(Point(p.lon, p.lat)):
                    votes[k] += 1
        if not votes:
            print(f"# ★{pref} {label}: どの地方の範囲にも入らない", file=sys.stderr)
            return 1
        ext = max(votes, key=votes.get)
        inside = [extracts[ext].contains(Point(p.lon, p.lat)) for p in pl]
        by_code: dict[str, list[bool]] = defaultdict(list)
        for c, ok in zip(code, inside, strict=True):
            by_code[c].append(ok)
        outside_all = sorted(c for c, v in by_code.items() if not any(v) and c not in exclude)
        partly = sorted(c for c, v in by_code.items() if any(v) and not all(v))
        exclude = sorted(set(exclude) | set(outside_all))

        keep = g[~code.isin(exclude)]
        zmap = zones(keep)
        by_zone: dict[int, list[str]] = defaultdict(list)
        for c, z in sorted(zmap.items()):
            by_zone[z].append(c)
        hits = [ext]

        subs = sorted(by_zone.items())
        for z, munis in subs:
            rname = name if len(subs) == 1 else f"{name}_z{z:02d}"
            rlabel = label if len(subs) == 1 else f"{label}（{z}系）"
            print(f"  {rname}:")
            print(f'    label: "{rlabel}"')
            print(f'    pref_codes: ["{pref}"]')
            if len(subs) == 1:
                print("    municipality_codes: null")
            else:
                print(f"    # ★平面直角座標系 {z} 系の市区町村（{len(munis)} 件）。NK-08")
                print("    municipality_codes: [" + ", ".join(f'"{c}"' for c in munis) + "]")
            if exclude:
                print("    # ★外す: 3km より離れた所属未定地／OSM の抽出の範囲外の区域")
                print("    exclude_municipality_codes: ["
                      + ", ".join(f'"{c}"' for c in exclude) + "]")
            else:
                print("    exclude_municipality_codes: null")
            print("    mesh_codes: null")
            print("    buffer_km: 3.0")
            print(f"    osm_extract: {hits[0]}")
        print(f"# {pref} {label}: 系 {[z for z, _ in subs]} / 地方 {hits[0]}"
              f" / 外す {exclude} / 一部が範囲外 {partly}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
