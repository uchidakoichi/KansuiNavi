"""地域の定義と、そこから決まるもののテスト。

★N03（行政区域データ）は引数で受け取る設計なので、**実データ無しで試せる。**
  ここでは小さな合成データで固定する。

守りたいのは:
  - 地域の指定を**コードに直書きしない**（藤沢版は `14205` と `6677` を埋めていた）
  - 系が決まらないときに**既定値へ丸めない**
  - ★北海道が複数系にまたがることを**例外として検出できる**（NK-08）
"""

from __future__ import annotations

import geopandas as gpd
import pytest
from shapely.geometry import Polygon

from kansui.crs import ZoneUnknownError
from kansui.region import (
    N03_CITY,
    N03_CODE,
    N03_PREF,
    N03_SUBPREF,
    Region,
    RegionError,
    hazard_url,
    mesh_epsg,
    mesh_zones,
    region_epsg,
    region_meshes,
    select,
    zones,
)


def _sq(lon: float, lat: float, d: float = 0.05) -> Polygon:
    return Polygon([(lon, lat), (lon + d, lat), (lon + d, lat + d), (lon, lat + d)])


def _n03(rows: list[tuple[str, str, str | None, str, float, float]]) -> gpd.GeoDataFrame:
    """(コード, 都道府県名, 振興局名, 市町村名, lon, lat) から N03 風の表を作る。"""
    return gpd.GeoDataFrame(
        {
            N03_CODE: [r[0] for r in rows],
            N03_PREF: [r[1] for r in rows],
            N03_SUBPREF: [r[2] for r in rows],
            N03_CITY: [r[3] for r in rows],
        },
        geometry=[_sq(r[4], r[5]) for r in rows],
        crs=4326,
    )


KANAGAWA = _n03([
    ("14205", "神奈川県", None, "藤沢市", 139.45, 35.32),
    ("14203", "神奈川県", None, "平塚市", 139.33, 35.31),
    ("14201", "神奈川県", None, "横浜市", 139.60, 35.44),
])

# ★北海道: XI系（小樽市）と XII系（札幌市）が隣り合う
HOKKAIDO = _n03([
    ("01203", "北海道", "後志総合振興局", "小樽市", 140.99, 43.18),
    ("01100", "北海道", "石狩振興局", "札幌市", 141.30, 43.05),
])


# -----------------------------------------------------------------------------
# Region の指定
# -----------------------------------------------------------------------------
def test_region_from_config_round_trip():
    r = Region.from_config({
        "name": "kanagawa", "pref_codes": ["14"],
        "municipality_codes": None, "mesh_codes": None, "buffer_km": 3.0,
    })
    assert r.name == "kanagawa"
    assert r.pref_codes == ("14",)
    assert r.municipality_codes is None
    assert r.buffer_km == 3.0


@pytest.mark.parametrize("bad", [1, "1", "014", "", None, "１４"])
def test_pref_code_must_be_two_ascii_digits(bad):
    """★`"01"`（北海道）を数値で渡させない。先頭の 0 が落ちる（T-16 と同じ筋）。"""
    with pytest.raises(RegionError):
        Region(name="x", pref_codes=(bad,))  # type: ignore[arg-type]


def test_municipality_codes_must_sit_inside_the_prefectures():
    """★県と市町村の指定が食い違っていたら止める。黙って空にしない。"""
    with pytest.raises(RegionError):
        Region(name="x", pref_codes=("14",), municipality_codes=("01100",))


def test_empty_pref_codes_is_refused():
    with pytest.raises(RegionError):
        Region(name="x", pref_codes=())


# -----------------------------------------------------------------------------
# N03 の絞り込み ── ★緯度経度で切らない
# -----------------------------------------------------------------------------
def test_select_by_prefecture():
    got = select(KANAGAWA, Region(name="k", pref_codes=("14",)))
    assert len(got) == 3


def test_select_by_municipality():
    """★藤沢版の「団体コードで抽出」をそのまま一般化したもの。"""
    got = select(KANAGAWA, Region(name="f", pref_codes=("14",),
                                  municipality_codes=("14205",)))
    assert list(got[N03_CODE]) == ["14205"]


def test_select_excludes_municipalities():
    """★東京都の島しょ部を外すための仕組み。外したものが残らないこと。"""
    got = select(KANAGAWA, Region(name="k", pref_codes=("14",),
                                  exclude_municipality_codes=("14205",)))
    assert "14205" not in list(got[N03_CODE])
    assert len(got) == 2


def test_exclude_code_missing_from_data_is_refused():
    """★外すと書いたコードがデータに無い＝書き間違い。黙って何も外さずに進まない。"""
    with pytest.raises(RegionError):
        select(KANAGAWA, Region(name="k", pref_codes=("14",),
                                exclude_municipality_codes=("14999",)))


def test_exclude_code_must_sit_inside_the_prefectures():
    """★13 と 14 の取り違えなど、対象外の県のコードを外すと書いても何も起きない。"""
    with pytest.raises(RegionError):
        Region(name="k", pref_codes=("14",), exclude_municipality_codes=("13361",))


def test_select_refuses_to_return_nothing():
    """★0行で先へ進めない。空の成果物ができてしまう。"""
    with pytest.raises(RegionError):
        select(KANAGAWA, Region(name="x", pref_codes=("01",)))


# -----------------------------------------------------------------------------
# 系の引き当て
# -----------------------------------------------------------------------------
def test_kanagawa_is_one_zone_and_matches_fujisawa():
    """★神奈川県は IX系のみ。藤沢版の EPSG:6677 と一致すること。"""
    sel = select(KANAGAWA, Region(name="k", pref_codes=("14",)))
    assert set(zones(sel).values()) == {9}
    assert region_epsg(sel) == 6677


def test_hokkaido_spans_several_zones_and_says_so():
    """★北海道はここで落ちるのが正しい。区画ごとに系を決める合図（NK-08）。"""
    sel = select(HOKKAIDO, Region(name="h", pref_codes=("01",)))
    assert set(zones(sel).values()) == {11, 12}
    with pytest.raises(ZoneUnknownError):
        region_epsg(sel)


# -----------------------------------------------------------------------------
# メッシュへの分割
# -----------------------------------------------------------------------------
def test_region_meshes_cover_the_polygons_not_the_bbox():
    """★bbox ではなく区域に重なるものだけ。

    離れた3市の bbox は広いが、返るのは実際に重なる区画だけになる。
    """
    sel = select(KANAGAWA, Region(name="k", pref_codes=("14",)))
    got = region_meshes(sel, level=2)
    assert got, "1枚も返らないのはおかしい"
    assert len(set(got)) == len(got), "同じ区画を二度返さない"
    # 藤沢市役所あたりの区画が含まれること
    assert "533903" in got or "533904" in got, got


def test_region_meshes_with_buffer_grows():
    sel = select(KANAGAWA, Region(name="k", pref_codes=("14",)))
    assert len(region_meshes(sel, 2, buffer_km=10.0)) > len(region_meshes(sel, 2))


def test_mesh_zones_flags_the_straddling_tile():
    """★北海道で、系の境界をまたぐ区画が見つかること。"""
    sel = select(HOKKAIDO, Region(name="h", pref_codes=("01",)))
    mz = mesh_zones(sel, level=2)
    assert mz, "区画が1つも出ない"
    assert any(len(v) == 1 for v in mz.values())
    # またぐ区画があるなら mesh_epsg は例外を投げること
    for code, zs in mz.items():
        if len(zs) > 1:
            with pytest.raises(ZoneUnknownError):
                mesh_epsg(code, zs)
            break


def test_mesh_epsg_refuses_to_guess():
    """★またいでいたら既定の系に丸めない。未決であることを例外で示す（NK-08）。"""
    with pytest.raises(ZoneUnknownError):
        mesh_epsg("644142", {11, 12})
    with pytest.raises(ZoneUnknownError):
        mesh_epsg("644142", set())
    assert mesh_epsg("533903", {9}) == 6677


# -----------------------------------------------------------------------------
# ハザードタイル
# -----------------------------------------------------------------------------
def test_hazard_url_is_built_from_the_prefecture_code():
    """★藤沢版は URL に `/14/` を直書きしていた。ここを引数にする。"""
    u = hazard_url("flood_l2", "14")
    assert "/14/" in u and "{pref}" not in u
    assert u.endswith("{z}/{x}/{y}.png")
    assert "/01/" in hazard_url("naisui", "01")


def test_hazard_url_refuses_unknown_layers():
    with pytest.raises(RegionError):
        hazard_url("tsunami", "14")
    with pytest.raises(RegionError):
        hazard_url("flood_l2", 14)  # type: ignore[arg-type]


def test_zones_take_the_zone_with_more_area_not_the_last_row():
    """★1つの団体が複数の系にまたがるとき、最後の行ではなく面積の多い系にする。

    2026-09-23: 薩摩川内市（本土 II系・甑島 I系）が、最後の行の I系に黙って寄っていた。
    """
    import geopandas as gpd
    from shapely.geometry import box

    from kansui.region import zones

    g = gpd.GeoDataFrame(
        {"N03_007": ["46215", "46215"], "N03_004": ["薩摩川内市", "薩摩川内市"],
         "N03_003": [None, None], "N03_002": [None, None], "N03_001": ["鹿児島県"] * 2},
        geometry=[box(130.20, 31.80, 130.40, 31.95),    # 本土（東経130度より東 → II系）・大きい
                  box(129.80, 31.70, 129.85, 31.75)],   # 甑島（東経130度より西 → I系）・小さい
        crs=4326,
    )
    assert zones(g) == {"46215": 2}


def test_amami_islands_extend_zone_one_to_130_13():
    """★奄美群島（奄美市・大島郡）は I系の東端が東経130度13分。喜界町（大島郡）の東端など。"""
    import geopandas as gpd
    from shapely.geometry import box

    from kansui.region import zones

    g = gpd.GeoDataFrame(
        {"N03_007": ["46529"], "N03_004": ["喜界町"], "N03_003": ["大島郡"],
         "N03_002": [None], "N03_001": ["鹿児島県"]},
        geometry=[box(130.01, 28.30, 130.03, 28.32)],   # 東経130度〜130度13分
        crs=4326,
    )
    assert zones(g) == {"46529": 1}
