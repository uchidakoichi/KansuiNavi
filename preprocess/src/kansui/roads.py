"""OSM から自動車道路ネットワークを取り出す。

★藤沢版（`../../Kansui/preprocess/src/kansui/roads.py`）を一般化したもの。
  違いは **bbox が `area.bbox` の直書きではなく `Region` の境界から来る**こと。

取得方式は pyrosm（ローカル PBF）。Overpass は使わない。
  OSM Wiki の Overpass「Limitations」が「リージョン全体（あるいはほぼ全体）の
  データを取りたい場合は planet.osm ミラーを使うべき」と明記しているため。

★**保持タグに `layer` を必ず含めること（T-01）。**
  osmnx の `settings.useful_tags_way` 既定にも pyrosm の `highway_columns` 既定にも
  `layer` は**入っていない**。既定に無いタグは `tags` という JSON 列へ押し込まれる。
  `layer` は T-01 対策の要であり、落とすと**対策が静かに機能しなくなる**。
  → 取得後に列の有無を検査して、無ければ**例外で止める**。

★**探索グラフは地域＋バッファで作る。** 地域で切ると迂回解が出なくなる。

★**一方通行と道路名は OSM にしか無い。** 国土地理院ベクトルタイルには入っていない
  （2026-09-19 調査）。幅員区分は地理院から載せるが、道路網は OSM が要る。

ライセンス: ODbL 1.0 / © OpenStreetMap contributors
  ★配布するグラフが Derivative Database に当たりうる（NK-11）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:  # pragma: no cover
    import geopandas as gpd
    from shapely.geometry.base import BaseGeometry

log = logging.getLogger(__name__)


class RoadsError(RuntimeError):
    """道路網が取り出せない。★空のまま先へ進まない。"""


def pbf_path_for_region(cfg, region_name: str) -> Path:
    """地域名 → 使う PBF のパス。"""
    # ★地域の定義に `osm_extract` があればそれを使う（tools/gen_regions.py が
    #   Geofabrik の範囲ポリゴンから決めて書く）。無ければ従来の対応表
    spec = (cfg.get("regions", {}) or {}).get(region_name) or {}
    mapping = cfg.get("osm.extract_for_region", {}) or {}
    key = spec.get("osm_extract") or mapping.get(region_name)
    if not key:
        raise RoadsError(
            f"地域 '{region_name}' に使う OSM 抽出が config に無い。"
            f"regions.{region_name}.osm_extract を書くこと"
        )
    extracts = cfg.require("osm.extracts")
    if key not in extracts:
        raise RoadsError(f"OSM 抽出 '{key}' が `osm.extracts` に無い")
    p = (Path(cfg.path).parent / extracts[key]["path"]).resolve()
    if not p.exists():
        raise RoadsError(
            f"PBF が見つからない: {p}\n  {extracts[key]['url']} から取得すること。"
            "★1回の HTTP ダウンロードなので、タイルの逐次取得とは性質が違う。"
        )
    return p


def buffered_poly(poly: BaseGeometry, buffer_km: float) -> BaseGeometry:
    """地域の境界＋バッファ（経緯度のポリゴン）。

    ★度で膨らませない。メートルで膨らませてから戻す（T-05 と同じ筋）。
    """
    import geopandas as gpd_

    if not buffer_km:
        return poly
    return (gpd_.GeoSeries([poly], crs=4326).to_crs(3857)
            .buffer(buffer_km * 1000.0).to_crs(4326).iloc[0])


def buffered_bbox(poly: BaseGeometry, buffer_km: float) -> tuple[float, float, float, float]:
    """地域の境界＋バッファの bbox（経緯度）。

    ★★**これは pyrosm に渡す「取り出し窓」であって、地域の形ではない。**
      bbox は矩形なので、神奈川県＋3km では**東京・山梨・静岡の奥まで含む。**
      取り出したあと、必ず `buffered_poly()` で**切り直すこと。**

      2026-09-21 に実際に踏んだ: 切り直していなかったため、
      エッジの **21.2%（307,282本・8,227km）が設計の 3km の外**にあった。
      ★黙って動く。経路は出るし、通信量と探索が重くなるだけなので気づけない。
      幅員タイルは**ポリゴン**で取っていたので、そこで食い違いが表に出た
      （県内の照合 94.0% に対し、県外 31.9%）。
    """
    return buffered_poly(poly, buffer_km).bounds


def extract_drivable(
    pbf: Path, bbox: tuple[float, float, float, float], cfg
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """自動車通行可能な道路のノード・エッジ。"""
    from pyrosm import OSM

    extra = list(cfg.require("osm.extra_attributes"))
    log.info("PBF から driving ネットワークを抽出（bbox=%s）",
             tuple(round(v, 5) for v in bbox))
    osm = OSM(str(pbf), bounding_box=list(bbox))
    nodes, edges = osm.get_network(
        network_type=cfg.require("osm.network_type"), nodes=True, extra_attributes=extra
    )
    if nodes is None or edges is None or len(edges) == 0:
        raise RoadsError("道路ネットワークが空。bbox か PBF を確認すること")

    missing = [t for t in extra if t not in edges.columns]
    for t in missing:
        # ★extra_attributes が効かないと T-01 対策が静かに壊れるので、列が無ければ確かめる。
        # ★★ただし「列が無い」には2通りある（2026-09-27、奄美・沖縄の離島で踏んだ）:
        #     (a) 読み取りの設定が効いていない … 道路にタグがあるのに落ちた → 止める
        #     (b) その範囲の道路に、そのタグが1本も無い … 列を「タグなし」で足して続ける
        #   → 同じ範囲をそのタグで**直接**引いて見分ける。推測で (b) に倒さない。
        if _tag_on_edges(osm, t, edges):
            raise RoadsError(
                f"必要なタグ列が取れなかった: {t}（抽出した道路にこのタグを持つ way がある）\n"
                "pyrosm の extra_attributes が効いていない。T-01 対策に必須なので続行しない。"
            )
        log.warning("★%s を持つ道路がこの範囲に1本も無い（直接引いて確認）。列を空で足す", t)
        edges[t] = None
    return nodes, edges


def _tag_on_edges(osm, tag: str, edges) -> bool:
    """抽出した道路の中に、`tag` を持つ way があるか。PBF を直接引き、**way ID で**突き合わせる。

    ★highway=* かどうかで見ない。屋根付きの歩道（highway=footway + covered=yes）のように、
      タグを持つが車の道として抽出されない way があり、それで誤って止まる（沖縄で実測）。
    """
    gdf = osm.get_data_by_custom_criteria(
        custom_filter={tag: True}, filter_type="keep",
        keep_nodes=False, keep_relations=False,
    )
    if gdf is None or len(gdf) == 0:
        return False
    if "id" not in gdf.columns or "id" not in edges.columns:
        return True   # ★突き合わせられないなら「効いていない」側に倒す
    return bool(set(gdf["id"].astype("int64")) & set(edges["id"].astype("int64")))


def extract_bridges(pbf: Path, bbox: tuple[float, float, float, float]) -> gpd.GeoDataFrame:
    """`bridge=*` の way を、道路・鉄道を問わず取り出す。

    ★T-01 の幾何推定に使う。
      OSM Wiki `Key:tunnel` は立体交差について「下の way を tunnel にするか、
      上の way を bridge にするか、**どちらか一方にすべきで両方はしない**」と定め、
      `JA:Key:layer` は「layer は tunnel/bridge/steps/elevator/covered を持つ way にのみ
      設定せよ」とする。つまり**橋の下をくぐる道路にはタグが付かないのが正規のタグ付け**。
      「自動車 way が bridge の way と平面交差しているのにノードを共有していない」箇所を
      拾うのが、タグの付かない立体交差を検出する唯一の手段になる。

      藤沢市域の実測では橋(946本)はトンネル(194本)の約4.9倍あった。
    """
    from pyrosm import OSM

    osm = OSM(str(pbf), bounding_box=list(bbox))
    gdf = osm.get_data_by_custom_criteria(
        custom_filter={"bridge": True}, filter_type="keep",
        keep_nodes=False, keep_relations=False, extra_attributes=["layer"],
    )
    if gdf is None or len(gdf) == 0:
        raise RoadsError("bridge=* の way が0件。T-01 の幾何推定ができない")
    return gdf


def assign_speeds(edges: gpd.GeoDataFrame, cfg) -> gpd.GeoDataFrame:
    """通過時間の算出用に速度を与える。

    ★藤沢市域で `maxspeed` を持つ way は **5.27%**（35,665本中1,879本）しかなかった。
      つまり `cost(e)` の第1項（通過時間）は実質的に**既定速度の与え方**が決めている。
      既定値は OSM Wiki のルーティング向け表（日本）に従う。実勢速度とは異なる。
    """
    defaults = cfg.require("osm.default_speed_kmh")
    fallback = float(defaults["_other"])

    def parse_maxspeed(v):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return np.nan
        s = str(v).strip().lower()
        try:
            if "mph" in s:
                return float(s.replace("mph", "").strip()) * 1.609344
            return float(s.split()[0])
        except (ValueError, IndexError):
            return np.nan   # "JP:urban" のような記号値。既定値へ回す

    parsed = edges["maxspeed"].map(parse_maxspeed) if "maxspeed" in edges.columns \
        else pd.Series(np.nan, index=edges.index)
    # ★有効範囲の外は誤記とみなす（→ config の osm.maxspeed_valid_kmh）。黙って捨てず件数を出す
    lo, hi = (float(x) for x in cfg.require("osm.maxspeed_valid_kmh"))
    bad = parsed.notna() & ((parsed < lo) | (parsed > hi))
    if bad.any():
        vals = sorted(set(parsed[bad].round(1).tolist()))
        log.warning("★maxspeed が有効範囲 %g〜%g km/h の外: %d 区間（値 %s）→ 既定速度へ回す",
                    lo, hi, int(bad.sum()), vals[:10])
        parsed = parsed.mask(bad)
    by_highway = edges["highway"].map(lambda h: float(defaults.get(h, fallback)))
    speed = pd.Series(parsed, index=edges.index).fillna(by_highway)

    out = edges.copy()
    out["speed_kmh"] = speed.astype("float32")
    out["speed_source"] = np.where(pd.Series(parsed, index=edges.index).notna(),
                                   "maxspeed", "default")
    out["travel_time_s"] = (out["length"] / (out["speed_kmh"] / 3.6)).astype("float32")
    return out
