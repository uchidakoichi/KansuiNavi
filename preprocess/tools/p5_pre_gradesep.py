"""P-5 の先行確認: 地理院ベクトルタイルだけで立体交差を拾えるか（藤沢・13地点）。

★藤沢版の公式「冠水注意箇所」13件は**すべて立体交差**である。
  これを地理院のタイルだけでどれだけ拾えるかを見る。

2つの手がかりを併せて使う（★片方では足りないことがこの測定で分かった）:
  ① 道路が `vt_code` 2704/2714（トンネル）── **地下道**を拾う
  ② 交差する鉄道が `vt_railstate` 「橋・高架」または `vt_lvorder` ≥ 1
     ── **高架下・アンダー**を拾う

★①だけでは 8/13、②だけでは 4/13。**併せて 12/13。**
  地下道と高架下は別の現象で、別の手がかりでしか見えない。

★これは**適合率の測定ではない。** 13地点は既知の正解であり、
  「見つかるか（感度）」しか測っていない。適合率は P-5 本番で無作為抽出して測る。
  参考として、無作為な市内の道で同じ条件が立つ率も併せて出す。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString

from kansui.gsi_vector import TUNNEL_CODES, VectorTiles, decode_layer

FUJISAWA = Path(__file__).resolve().parents[3] / "Kansui/preprocess/data/raw"  # 藤沢版のリポジトリを隣に置いた前提
CACHE = Path("data/cache/xyz")
Z = 16
RADIUS_M = 40.0        # 地点からこの距離内に手がかりがあるか
RAIL_LAYERS = ("RailCL", "RailTrCL")
RAIL_ELEVATED = "橋・高架"


def _cached_tiles() -> list[tuple[int, int]]:
    out = []
    for p in (CACHE / f"optimal_bvmap-v1/{Z}").rglob("*.pbf"):
        m = re.search(rf"/{Z}/(\d+)/(\d+)\.pbf$", str(p))
        if m:
            out.append((int(m[1]), int(m[2])))
    return out


def main() -> None:
    # ★キャッシュから読むだけ。新しく取りに行かない（絶対ルール3）
    vt = VectorTiles(cache_dir=CACHE, user_agent="cache-only", sleep_sec=0.0)
    tiles = _cached_tiles()
    print(f"キャッシュ済みタイル {len(tiles)} 枚")

    parts = []
    for layer in RAIL_LAYERS:
        geoms, props = [], []
        for x, y in tiles:
            data = vt.fetch(Z, x, y)
            if data is None:
                continue
            for lines, p in decode_layer(data, Z, x, y, layer):
                for ln in lines:
                    geoms.append(LineString(ln))
                    props.append({**p, "layer": layer})
        if geoms:
            parts.append(gpd.GeoDataFrame(props, geometry=geoms, crs=4326).to_crs(6677))
    assert vt.stats["fetched"] == 0, "★キャッシュがあるのに取りに行った"

    rail = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=6677)
    roads = gpd.read_parquet("data/raw/gsi_rdcl_fujisawa.parquet").to_crs(6677)
    known = gpd.read_file(FUJISAWA / "roads" / "known_snap.geojson").to_crs(6677)

    print(f"\n{'地点':<25}{'道路code':<15}{'鉄道の状態':<20}{'鉄道lv':<9}判定")
    hit = tun_n = elev_n = 0
    for _, r in known.iterrows():
        near_rd = roads[roads.distance(r.geometry) <= RADIUS_M]
        near_rl = rail[rail.distance(r.geometry) <= RADIUS_M]
        codes = sorted(set(near_rd["vt_code"]))
        states = sorted({str(s) for s in near_rl["vt_railstate"].dropna()}) if len(near_rl) else []
        lvs = sorted({int(v) for v in near_rl["vt_lvorder"].dropna()}) if len(near_rl) else []

        tunnel = any(c in TUNNEL_CODES for c in codes)
        elevated = (RAIL_ELEVATED in states) or any(v >= 1 for v in lvs)
        ok = tunnel or elevated
        hit += ok
        tun_n += tunnel
        elev_n += elevated
        mark = ("トンネル" if tunnel else "") + ("/高架" if elevated else "")
        print(f"{str(r.get('name') or '')[:23]:<25}{str(codes):<15}{str(states):<20}"
              f"{str(lvs):<9}{'○ ' + mark if ok else '×'}")

    print(f"\n★拾えた: {hit}/{len(known)} ({hit / len(known):.0%})"
          f"  （道路トンネル {tun_n} 件 / 鉄道の高架 {elev_n} 件）")
    print("  ★①だけ・②だけでは足りない。地下道と高架下は別の手がかりでしか見えない。")


if __name__ == "__main__":
    sys.exit(main())
