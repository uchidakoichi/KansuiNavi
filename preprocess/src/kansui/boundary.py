"""行政区域（国土数値情報 N03）の取得と読み込み。

★藤沢版は「藤沢市（団体コード 14205）」を1つ取り出すだけだった。
  全国版は **`Region` の指定に従って複数都道府県・複数市町村**を扱う。

━━ 一次情報 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  国土数値情報 行政区域データ N03
    https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-N03-2024.html
  ライセンス: **オープンデータ（CC BY 4.0）**
  ファイル名の規則: `N03-YYYYMMDD_[都道府県コード]_GML.zip`
    ★全国一括もある（`N03-20260101_GML.zip`。2026-09-20 実測で **766.0MB**）
    ★都道府県別は 神奈川県で **5.1MB**（同日実測）
  属性（公式の説明のまま）:
    N03_001 都道府県名
    N03_002 **北海道の振興局名** ── 「支庁のうち、都道府県が『北海道』である場合の振興局名」
    N03_003 郡名
    N03_004 市区町村名
    N03_005 政令指定都市の行政区名
    N03_007 全国地方公共団体コード（JIS X 0401/0402 に基づく5桁）
  取得日: 2026年9月20日
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

★**N03_002 が「北海道の振興局名」であることは公式の定義。** `kansui.crs` が
  北海道の系を振興局名で引き当てている前提は、ここで裏が取れている。

★**都道府県別を使う。** 全国一括 766MB を落とすのは、1地域ずつ流す設計（Phase 1/2）に
  対して無駄が大きい。必要な県だけ取る（絶対ルール3 の精神）。

出典表示（アプリに必須）:
  「出典：国土交通省国土数値情報ダウンロードサイト」
  「「国土数値情報（行政区域データ）」（国土交通省）をもとに加工」
"""

from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from pyproj import Geod
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from kansui.region import N03_CODE, Region

if TYPE_CHECKING:  # pragma: no cover
    import geopandas as gpd

log = logging.getLogger(__name__)

#: GRS80 楕円体。JGD2011/JGD2024 の準拠楕円体。
#: ★面積は**測地線ベース**で測る。平面近似だと緯度で歪む（T-05 と同じ筋）。
_GEOD = Geod(ellps="GRS80")

ATTRIBUTION = "「国土数値情報（行政区域データ）」（国土交通省）をもとに加工"
SOURCE_NOTE = "出典：国土交通省国土数値情報ダウンロードサイト"


class BoundaryError(RuntimeError):
    """行政区域が読めない・検算に通らない。★推測で先に進まない。"""


def zip_url(template: str, pref_code: str) -> str:
    """都道府県コードから N03 の ZIP URL を組み立てる。"""
    return template.replace("{pref}", pref_code)


def fetch_prefecture(fetcher, url_template: str, pref_code: str, dest_dir: Path) -> Path:
    """1都道府県ぶんの N03 を取る。★キャッシュにあれば取りに行かない。"""
    url = zip_url(url_template, pref_code)
    dest = Path(dest_dir) / Path(url).name
    return fetcher.fetch_file(url, dest)


def read_zip(zip_path: Path) -> list[dict]:
    """N03 の ZIP から GeoJSON のフィーチャを読む。"""
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".geojson")]
        if not names:
            raise BoundaryError(
                f"{Path(zip_path).name} に .geojson が無い: {zf.namelist()}"
            )
        with zf.open(names[0]) as f:
            fc = json.load(f)
    feats = fc.get("features") or []
    if not feats:
        raise BoundaryError(f"{Path(zip_path).name} のフィーチャが 0 件")
    return feats


def to_geodataframe(features: list[dict]) -> gpd.GeoDataFrame:
    """N03 のフィーチャ → GeoDataFrame（EPSG:6668）。

    ★団体コードは**文字列のまま**持つ。数値にすると `01100`（札幌市）の
      先頭の 0 が落ちる（T-16 と同じ筋）。
    """
    import geopandas as gpd_

    props = [f["properties"] for f in features]
    for p in props:
        if p.get(N03_CODE) is not None:
            p[N03_CODE] = str(p[N03_CODE])
    return gpd_.GeoDataFrame(
        props, geometry=[shape(f["geometry"]) for f in features], crs=6668
    )


def load(fetcher, url_template: str, region: Region, cache_dir: Path) -> gpd.GeoDataFrame:
    """`Region` の指定に従って行政区域を読み込む。

    ★取得は**都道府県ごとに逐次**（絶対ルール3）。fetcher が sleep とキャッシュを持つ。
    """
    import geopandas as gpd_
    import pandas as pd

    parts = []
    for code in region.pref_codes:
        zp = fetch_prefecture(fetcher, url_template, code, cache_dir)
        gdf = to_geodataframe(read_zip(zp))
        log.info("N03 %s: %d フィーチャ", code, len(gdf))
        parts.append(gdf)
    all_gdf = gpd_.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=6668)

    from kansui.region import select

    return select(all_gdf, region)


# -----------------------------------------------------------------------------
# 検算
# -----------------------------------------------------------------------------
def geodesic_area_km2(geom: BaseGeometry) -> float:
    """測地線ベースの面積［km²］。★平面近似しない。"""
    return abs(_GEOD.geometry_area_perimeter(geom)[0]) / 1e6


def check_area(
    gdf: gpd.GeoDataFrame, code: str, official_km2: float, tolerance: float = 0.01
) -> float:
    """ある団体コードの面積を公式値と突き合わせる。

    ★**読み込みと座標系が正しいかの回帰検知。** 合わなければ例外にして止める。
      「だいたい合っている」で先に進むと、以降の距離計算が全部ずれる。
    """
    sub = gdf[gdf[N03_CODE].astype(str) == str(code)]
    if sub.empty:
        raise BoundaryError(f"団体コード {code} が読み込んだ区域に無い")
    got = geodesic_area_km2(sub.union_all())
    diff = abs(got - official_km2) / official_km2
    if diff > tolerance:
        raise BoundaryError(
            f"面積の検算に失敗: {code} は算出 {got:.3f} km² vs 公式 {official_km2} km² "
            f"(差 {diff:.2%} > 許容 {tolerance:.0%})。\n"
            "抽出条件かデータソースが変わった可能性がある。推測で先に進まないこと。"
        )
    log.info("面積検算OK: %s 算出 %.2f km² vs 公式 %.2f km² (差 %.2f%%)",
             code, got, official_km2, diff * 100)
    return got
