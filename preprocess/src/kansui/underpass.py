"""アンダーパス（立体交差の下側）の幾何推定。★T-01 対策の中核。

★藤沢版（`../../Kansui/preprocess/src/kansui/underpass.py`）を一般化したもの。
  違いは **EPSG を引数で受ける**こと（藤沢版は 6677 を直書きして検査していた）。
  距離で頂点一致を見るので、**必ずその地域の平面直角座標系**を渡す（T-05）。


なぜ幾何推定が必要か:
  OSM Wiki Key:tunnel は立体交差について「下のwayを tunnel にするか、上のwayを bridge に
  するか、**どちらか一方にすべきで両方はしない**」と定め、JA:Key:layer は
  「layer は tunnel/bridge/steps/elevator/covered を持つwayにのみ設定せよ」とする。
  つまり**橋の下をくぐる道路にはタグが付かないのが正規のタグ付け**である。

  実測（★藤沢市域+3km、highway way 177,542本）:
      bridge 2,964本 / layer 3,228本 / tunnel 215本 / covered 2本
  橋はトンネルの13.8倍あり、タグだけを見ると立体交差の下側を大量に取りこぼす。

  さらに Step 1 の実測で、既知アンダーパス2件の Δh は
      湘南辻堂地下道 4.16m（全道路中で下から54.7%tile）
      高山地下道     8.67m（下から27%tile＝最も安全な部類）
  であり、**地形からは全く検出できない**ことが確認された。
  DEM は跨線橋の路面標高を記録するためで、分解能を上げても解決しない。

判定ロジック:
  自動車way と bridge=* の way（鉄道を含む）が平面交差しているが、
  その交点が**どちらのwayの頂点でもない**とき、立体交差の下側とみなす。
  OSM では2つのwayが接続する場合は必ずノード（＝頂点）を共有するため、
  「交差しているのに頂点を共有しない」＝ 平面交差していない ＝ 立体交差、が成り立つ。
"""

from __future__ import annotations

import logging

import geopandas as gpd
import numpy as np
from shapely.geometry import MultiPoint, Point
from shapely.ops import unary_union

log = logging.getLogger(__name__)

VERTEX_TOL_M = 0.5   # 頂点との一致とみなす距離。座標の丸め誤差を吸収する

# ★上を通るものがこれらなら、下の道路は「アンダーパス」ではない。
#   歩道橋の下の道路は路面が下がっておらず、水を溜める構造ではない。
#   実測（藤沢市域）: 交差地点216のうち93地点（43%）が歩行者用の橋の下だった。
#   除外しないと 180本 → 実質112本 のところを 68本ぶん過大に警告することになる。
UPPER_EXCLUDE_HIGHWAY = frozenset(
    {"footway", "pedestrian", "path", "steps", "cycleway", "construction", "proposed"}
)

# 根拠の強さ。表示ラベルを分けるために使う（「わからない」を「危険」に丸めない方針）
EVIDENCE_CONFIRMED = "underpass_confirmed"   # tunnel タグ or 高さ制限あり
EVIDENCE_RAILWAY = "under_railway"           # 鉄道の下。典型的なアンダーパス形状
EVIDENCE_VIADUCT = "under_road_viaduct"      # 道路高架の下。路面が下がっていない場合もある


def _vertices(geom) -> np.ndarray:
    if geom.geom_type == "MultiLineString":
        return np.vstack([np.asarray(g.coords) for g in geom.geoms])
    return np.asarray(geom.coords)


def _is_vertex_of(pt: Point, geom, tol: float = VERTEX_TOL_M) -> bool:
    v = _vertices(geom)
    return bool((np.hypot(v[:, 0] - pt.x, v[:, 1] - pt.y) <= tol).any())


def find_grade_separations(
    edges: gpd.GeoDataFrame, bridges: gpd.GeoDataFrame, epsg: int,
    tol: float = VERTEX_TOL_M,
) -> gpd.GeoDataFrame:
    """自動車wayが橋の下をくぐっている地点を返す。

    ★`epsg` は**その地域の平面直角座標系**（EPSG:6669〜6687）。頂点判定に距離を使うので、
      経緯度や Web メルカトルを渡してはいけない（T-05 / T-10）。
    ★**CRS を名前で比較せず EPSG コードで比較する**（2025年4月に JGD2011 → JGD2024 と改称。
      定義・座標値は不変）。
    """
    from kansui.crs import zone_of_epsg

    zone_of_epsg(epsg)  # ★平面直角座標系でなければここで ValueError
    for name, gdf in (("edges", edges), ("bridges", bridges)):
        if gdf.crs is None or gdf.crs.to_epsg() != epsg:
            raise ValueError(f"{name} の CRS が EPSG:{epsg} ではない: {gdf.crs}")

    log.info("交差候補を空間結合で抽出（edges %d × bridges %d）", len(edges), len(bridges))
    pairs = gpd.sjoin(
        edges[["geometry"]].reset_index(names="edge_idx"),
        bridges[["geometry"]].reset_index(names="bridge_idx"),
        predicate="intersects",
    )
    log.info("交差するペア: %d 件。頂点共有の有無で篩い分ける", len(pairs))

    recs = []
    egeom = edges.geometry.to_numpy()
    bgeom = bridges.geometry.to_numpy()
    for ei, bi in zip(pairs["edge_idx"].to_numpy(), pairs["bridge_idx"].to_numpy(), strict=True):
        eg, bg = egeom[ei], bgeom[bi]
        inter = eg.intersection(bg)
        if inter.is_empty:
            continue
        pts = (
            [inter] if inter.geom_type == "Point"
            else list(inter.geoms) if isinstance(inter, MultiPoint)
            else [p for p in getattr(inter, "geoms", []) if p.geom_type == "Point"]
        )
        for p in pts:
            # ★どちらかの頂点なら、そこは実際に接続している交差点（平面交差）
            if _is_vertex_of(p, eg, tol) or _is_vertex_of(p, bg, tol):
                continue
            recs.append({"edge_idx": int(ei), "bridge_idx": int(bi),
                         "x": p.x, "y": p.y, "geometry": p})

    if not recs:
        return gpd.GeoDataFrame(
            columns=["edge_idx", "bridge_idx", "x", "y", "geometry"],
            geometry="geometry", crs=epsg,
        )
    return gpd.GeoDataFrame(recs, geometry="geometry", crs=epsg)


def merge_with_tags(seps: gpd.GeoDataFrame, edges: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """幾何推定の結果に、下側wayのタグ（tunnel/layer など）を突き合わせる。

    タグと幾何のどちらで検出できたかを分けて数えると、T-01 の取りこぼし量が分かる。
    """
    cols = [c for c in ("name", "highway", "tunnel", "layer", "bridge", "covered", "maxheight")
            if c in edges.columns]
    out = seps.merge(
        edges[cols].reset_index(names="edge_idx"), on="edge_idx", how="left", suffixes=("", "_edge")
    )
    out["has_tunnel_tag"] = out["tunnel"].notna() if "tunnel" in out.columns else False
    out["has_negative_layer"] = (
        out["layer"].apply(lambda v: str(v).lstrip("-").isdigit() and str(v).startswith("-"))
        if "layer" in out.columns else False
    )
    out["tagged"] = out["has_tunnel_tag"] | out["has_negative_layer"]
    return out


def classify_upper(bridges: gpd.GeoDataFrame, bridge_idx) -> str:
    """立体交差の上を通っているものの種別を返す。'exclude' なら下はアンダーパスではない。"""
    import json as _json

    row = bridges.iloc[int(bridge_idx)]
    raw = row.get("tags")
    try:
        tags = _json.loads(raw) if isinstance(raw, str) and raw else {}
    except ValueError:
        tags = {}
    if "railway" in tags:
        return "railway"
    hw = tags.get("highway")
    if hw in UPPER_EXCLUDE_HIGHWAY:
        return "exclude"
    return "road" if hw else "unknown"


# 上を通るものの優先順位。1つのエッジが複数の橋と交差する場合に使う。
# ★「最初に一致したもの」で決めると、鉄道と道路の両方をくぐる地点が
#   道路扱いになって根拠が過小評価される（実際に No.34 湘南台第一地下道で発生した）。
UPPER_PRIORITY = ("railway", "road", "unknown", "exclude")


def dominant_upper(kinds) -> str:
    """複数の交差がある場合、最も確実な根拠を選ぶ。鉄道 > 道路。"""
    ks = set(kinds)
    for k in UPPER_PRIORITY:
        if k in ks:
            return k
    return "unknown"


def cluster_points(gdf: gpd.GeoDataFrame, radius_m: float = 30.0) -> gpd.GeoDataFrame:
    """近接する交差点をまとめる。1つの立体交差が複数のway対で重複検出されるため。

    ★半径はメートル。**その地域の平面直角座標系**で渡されている前提（T-05）。
    """
    if len(gdf) == 0:
        return gdf
    merged = unary_union(gdf.geometry.buffer(radius_m))
    polys = list(merged.geoms) if merged.geom_type == "MultiPolygon" else [merged]
    clusters = gpd.GeoDataFrame({"cluster": range(len(polys))}, geometry=polys, crs=gdf.crs)
    joined = gpd.sjoin(gdf, clusters, predicate="within", how="left")
    return joined
