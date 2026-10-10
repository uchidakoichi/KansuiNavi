"""国土地理院 最適化ベクトルタイルの取得・復号の単体テスト（通信しない）。

★守りたいのは、やはり「静かに壊れる」形:
  - MVT のタイル内ローカル座標を経緯度へ直し忘れる／上下が反転する
    → **タイルの範囲内には収まるので bbox 検査では気づけない。**
      道が数百メートルずれたまま幅員が別の道から付く
  - 幅員区分の帯に隙間や重なりができ、実測値がどこにも入らなくなる
  - キャッシュが効かず同じタイルを二度取りに行く（絶対ルール3）
"""

from __future__ import annotations

import math

import pytest

from kansui.gsi_vector import (
    BRIDGE_CODES,
    CODE_ROAD_BRIDGE,
    CODE_ROAD_TUNNEL,
    MAX_ZOOM,
    RNKWIDTH_RANGE,
    TUNNEL_CODES,
    VectorTiles,
    decode_layer,
    lonlat_to_tile,
    tile_to_lonlat,
    tiles_for_bbox,
)

# 藤沢市役所付近。★実際に取得して中身を確認した地点（2026-09-20）
FUJISAWA_LON, FUJISAWA_LAT = 139.4914, 35.3392


# -----------------------------------------------------------------------------
# タイル番号
# -----------------------------------------------------------------------------
def test_tile_number_round_trip():
    """タイル番号 → 左上角 → タイル番号 が戻ること。"""
    for z in (11, 14, MAX_ZOOM):
        x, y = lonlat_to_tile(FUJISAWA_LON, FUJISAWA_LAT, z)
        lon, lat = tile_to_lonlat(x, y, z)
        # 左上角はそのタイルに属する（角の丸めで隣に行かないこと）
        assert lonlat_to_tile(lon + 1e-9, lat - 1e-9, z) == (x, y)


def test_tile_side_is_about_500m_at_z16_in_fujisawa():
    """★藤沢の緯度で z16 のタイルは約 500m。

    枚数の見積もり（353枚 ≒ 6分）がこの前提に乗っている。
    """
    x, y = lonlat_to_tile(FUJISAWA_LON, FUJISAWA_LAT, MAX_ZOOM)
    w, n = tile_to_lonlat(x, y, MAX_ZOOM)
    e, _ = tile_to_lonlat(x + 1, y, MAX_ZOOM)
    side_m = (e - w) * 111_320.0 * math.cos(math.radians(n))
    assert 450 < side_m < 550, side_m


def test_tiles_for_bbox_covers_both_corners():
    x0, y0 = lonlat_to_tile(139.40, 35.43, MAX_ZOOM)
    x1, y1 = lonlat_to_tile(139.51, 35.30, MAX_ZOOM)
    tiles = tiles_for_bbox(139.40, 35.30, 139.51, 35.43, MAX_ZOOM)
    assert (x0, y0) in tiles and (x1, y1) in tiles
    assert len(tiles) == (x1 - x0 + 1) * (y1 - y0 + 1)
    assert len(set(tiles)) == len(tiles), "同じタイルを二度並べない"


# -----------------------------------------------------------------------------
# 幅員区分の帯
# -----------------------------------------------------------------------------
def test_rnkwidth_bands_match_the_published_spec():
    """区分は仕様書（optbv_attribute.pdf）の文言どおり。

    ★勝手に丸めたり足したりしない。「その他」「不明」は範囲を持たないので
      この表に入れない。
    """
    assert list(RNKWIDTH_RANGE) == [
        "3m未満", "3m-5.5m未満", "5.5m-13m未満", "13m-19.5m未満", "19.5m以上",
    ]
    assert RNKWIDTH_RANGE["3m未満"] == (None, 3.0)
    assert RNKWIDTH_RANGE["19.5m以上"] == (19.5, None)


def test_rnkwidth_bands_are_contiguous_without_gaps_or_overlaps():
    """★帯に隙間や重なりがあると、実測値がどの区分にも入らない／二重に入る。"""
    bands = list(RNKWIDTH_RANGE.values())
    for (_, hi), (lo, _) in zip(bands, bands[1:], strict=False):
        assert hi == lo, f"帯が連続していない: {hi} → {lo}"


@pytest.mark.parametrize(
    ("width_m", "expected"),
    [
        (2.9, "3m未満"),
        (3.0, "3m-5.5m未満"),    # 境界値は上の帯に入る（「3m未満」ではない）
        (5.49, "3m-5.5m未満"),
        (5.5, "5.5m-13m未満"),
        (12.99, "5.5m-13m未満"),
        (13.0, "13m-19.5m未満"),
        (19.49, "13m-19.5m未満"),
        (19.5, "19.5m以上"),
        (40.0, "19.5m以上"),
    ],
)
def test_band_boundaries_are_half_open_upward(width_m, expected):
    hit = [
        k for k, (lo, hi) in RNKWIDTH_RANGE.items()
        if (lo is None or width_m >= lo) and (hi is None or width_m < hi)
    ]
    assert hit == [expected], f"{width_m}m → {hit}"


# -----------------------------------------------------------------------------
# 地物種別コード
# -----------------------------------------------------------------------------
def test_bridge_and_tunnel_codes_are_distinct():
    """★T-01 の手がかり。橋とトンネルを取り違えない。"""
    assert CODE_ROAD_BRIDGE == 2703
    assert CODE_ROAD_TUNNEL == 2704
    assert not (BRIDGE_CODES & TUNNEL_CODES)
    assert 2713 in BRIDGE_CODES and 2714 in TUNNEL_CODES, "２条道路ぶんも入れる"


# -----------------------------------------------------------------------------
# 復号 ── ★タイル内ローカル座標 → 経緯度
# -----------------------------------------------------------------------------
def _encode(coords: list[tuple[float, float]], extent: int = 4096) -> bytes:
    import mapbox_vector_tile as mvt

    return mvt.encode(
        {"name": "RdCL",
         "features": [{"geometry": {"type": "LineString", "coordinates": coords},
                       "properties": {"vt_rnkwidth": "5.5m-13m未満"}}]},
        default_options={"extents": extent},
    )


def test_decode_maps_tile_corners_to_the_right_lonlat():
    """★上下が反転していないこと。

    MVT のローカル原点は左下（mapbox_vector_tile の既定 y_coord_down=False）。
    反転していても**タイルの範囲内には収まる**ので、bbox を見るだけでは
    絶対に気づけない。角を名指しで固定する。
    """
    z = MAX_ZOOM
    x, y = lonlat_to_tile(FUJISAWA_LON, FUJISAWA_LAT, z)
    west, north = tile_to_lonlat(x, y, z)
    east, south = tile_to_lonlat(x + 1, y + 1, z)

    feats = decode_layer(_encode([(0, 0), (4096, 4096)]), z, x, y)
    assert len(feats) == 1
    (line,), props = feats[0]
    assert props["vt_rnkwidth"] == "5.5m-13m未満"

    (lon0, lat0), (lon1, lat1) = line[0], line[-1]
    assert lon0 == pytest.approx(west, abs=1e-9)
    assert lat0 == pytest.approx(south, abs=1e-7), "★ローカル y=0 はタイルの下端"
    assert lon1 == pytest.approx(east, abs=1e-9)
    assert lat1 == pytest.approx(north, abs=1e-7), "★ローカル y=extent はタイルの上端"


def test_decode_ignores_layers_and_geometry_types_it_does_not_handle():
    z = MAX_ZOOM
    x, y = lonlat_to_tile(FUJISAWA_LON, FUJISAWA_LAT, z)
    assert decode_layer(_encode([(0, 0), (10, 10)]), z, x, y, layer="RailCL") == []


# -----------------------------------------------------------------------------
# 取得 ── 絶対ルール3
# -----------------------------------------------------------------------------
def test_cache_hit_does_not_touch_the_network(tmp_path, monkeypatch):
    """同じタイルを二度取りに行かないこと。"""
    vt = VectorTiles(cache_dir=tmp_path, user_agent="test", sleep_sec=0.0)
    p = tmp_path / "optimal_bvmap-v1/16/1/2.pbf"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"cached")

    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("★キャッシュがあるのに取りに行った")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert vt.fetch(16, 1, 2) == b"cached"
    assert vt.stats["cache_hits"] == 1
    assert vt.stats["fetched"] == 0


def test_missing_tile_is_remembered_as_empty(tmp_path, monkeypatch):
    """★404 を印として残す。残さないとデータの無い区画を毎回叩きに行く。"""
    vt = VectorTiles(cache_dir=tmp_path, user_agent="test", sleep_sec=0.0)
    p = tmp_path / "optimal_bvmap-v1/16/3/4.pbf"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"")

    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("★無いと分かっているのに取りに行った")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert vt.fetch(16, 3, 4) is None
