"""行政区域（N03）の読み込みのテスト（通信しない）。

★守りたいのは:
  - 団体コードが**数値に化けない**（`01100` の先頭 0 が落ちる。T-16 と同じ筋）
  - 面積の検算が**本当に止める**（「だいたい合っている」で先へ進むと距離が全部ずれる）
  - 0件のまま先へ進まない
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from shapely.geometry import Polygon, mapping

from kansui.boundary import (
    BoundaryError,
    check_area,
    geodesic_area_km2,
    read_zip,
    to_geodataframe,
    zip_url,
)
from kansui.region import N03_CODE

URL_TEMPLATE = "https://nlftp.mlit.go.jp/ksj/gml/data/N03/N03-2026/N03-20260101_{pref}_GML.zip"


def _feature(code, name, geom, subpref=None):
    return {
        "type": "Feature",
        "properties": {"N03_001": "テスト県", "N03_002": subpref,
                       "N03_004": name, "N03_007": code},
        "geometry": mapping(geom),
    }


def _zip(features, tmp_path: Path, name="N03.zip") -> Path:
    p = tmp_path / name
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("N03-20260101_14.geojson",
                   json.dumps({"type": "FeatureCollection", "features": features}))
    p.write_bytes(buf.getvalue())
    return p


def test_zip_url_inserts_the_prefecture_code():
    assert zip_url(URL_TEMPLATE, "14").endswith("N03-20260101_14_GML.zip")
    assert zip_url(URL_TEMPLATE, "01").endswith("N03-20260101_01_GML.zip")


def test_read_zip_refuses_a_zip_without_geojson(tmp_path):
    p = tmp_path / "x.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "hi")
    p.write_bytes(buf.getvalue())
    with pytest.raises(BoundaryError, match="geojson"):
        read_zip(p)


def test_read_zip_refuses_zero_features(tmp_path):
    """★空のまま先へ進まない。空の成果物ができてしまう。"""
    with pytest.raises(BoundaryError):
        read_zip(_zip([], tmp_path))


def test_codes_stay_strings_with_their_leading_zero(tmp_path):
    """★`01100`（札幌市）を数値にすると 1100 になる。T-16 と同じ筋。

    GeoJSON の中で**数値として**書かれていても、文字列に直すこと。
    """
    sq = Polygon([(141.3, 43.0), (141.4, 43.0), (141.4, 43.1), (141.3, 43.1)])
    feats = [_feature("01100", "札幌市", sq, "石狩振興局")]
    feats[0]["properties"][N03_CODE] = 1100  # ★数値で入っていた場合
    gdf = to_geodataframe(read_zip(_zip(feats, tmp_path)))
    assert isinstance(gdf[N03_CODE].iloc[0], str)


def test_geodataframe_is_jgd2011_geographic(tmp_path):
    sq = Polygon([(139.4, 35.3), (139.5, 35.3), (139.5, 35.4), (139.4, 35.4)])
    gdf = to_geodataframe(read_zip(_zip([_feature("14205", "藤沢市", sq)], tmp_path)))
    assert gdf.crs.to_epsg() == 6668
    assert gdf["N03_004"].iloc[0] == "藤沢市"


# -----------------------------------------------------------------------------
# 面積の検算 ── ★読み込みと座標系が正しいかの回帰検知
# -----------------------------------------------------------------------------
def test_geodesic_area_is_not_a_planar_approximation():
    """★測地線ベース。平面近似だと緯度で歪む（T-05 と同じ筋）。

    同じ「0.1度四方」でも、北にあるほど東西方向が縮んで面積が小さくなる。
    """
    south = Polygon([(139.4, 30.0), (139.5, 30.0), (139.5, 30.1), (139.4, 30.1)])
    north = Polygon([(139.4, 43.0), (139.5, 43.0), (139.5, 43.1), (139.4, 43.1)])
    assert geodesic_area_km2(south) > geodesic_area_km2(north) * 1.1


def test_check_area_passes_within_tolerance(tmp_path):
    sq = Polygon([(139.4, 35.3), (139.5, 35.3), (139.5, 35.4), (139.4, 35.4)])
    gdf = to_geodataframe(read_zip(_zip([_feature("14205", "藤沢市", sq)], tmp_path)))
    truth = geodesic_area_km2(sq)
    assert check_area(gdf, "14205", truth, 0.01) == pytest.approx(truth)


def test_check_area_stops_when_it_does_not_match(tmp_path):
    """★**止まること**が要件。「だいたい合っている」で先に進まない。"""
    sq = Polygon([(139.4, 35.3), (139.5, 35.3), (139.5, 35.4), (139.4, 35.4)])
    gdf = to_geodataframe(read_zip(_zip([_feature("14205", "藤沢市", sq)], tmp_path)))
    with pytest.raises(BoundaryError, match="面積の検算に失敗"):
        check_area(gdf, "14205", geodesic_area_km2(sq) * 1.5, 0.01)


def test_check_area_refuses_a_missing_code(tmp_path):
    sq = Polygon([(139.4, 35.3), (139.5, 35.3), (139.5, 35.4), (139.4, 35.4)])
    gdf = to_geodataframe(read_zip(_zip([_feature("14205", "藤沢市", sq)], tmp_path)))
    with pytest.raises(BoundaryError, match="14201"):
        check_area(gdf, "14201", 100.0)


def test_subprefecture_survives_so_hokkaido_zones_can_be_resolved(tmp_path):
    """★N03_002 は公式に「北海道の振興局名」。crs.py がこれで系を引く。"""
    from kansui.region import zones

    otaru = Polygon([(140.98, 43.17), (141.02, 43.17), (141.02, 43.21), (140.98, 43.21)])
    sapporo = Polygon([(141.28, 43.02), (141.32, 43.02), (141.32, 43.06), (141.28, 43.06)])
    gdf = to_geodataframe(read_zip(_zip([
        _feature("01203", "小樽市", otaru, "後志総合振興局"),
        _feature("01100", "札幌市", sapporo, "石狩振興局"),
    ], tmp_path)))
    assert zones(gdf) == {"01203": 11, "01100": 12}
