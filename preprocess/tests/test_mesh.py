"""標準地域メッシュの単体テスト。

★守りたいのは「静かに1区画ずれる」こと。
  境界の扱い（半開区間）や隣の求め方を間違えても例外は出ず、
  **配る区画がずれたまま**になる。経路が切れて初めて気づく。

一次情報: 総務省統計局「地域メッシュ統計について」／区分方法（PDF）
          根拠 昭和48年7月12日行政管理庁告示第143号
          https://www.stat.go.jp/data/mesh/m_tuite.html （2026-09-20 取得）
"""

from __future__ import annotations

import pytest

from kansui.mesh import (
    LAT2,
    LON2,
    MeshCodeError,
    mesh_bbox,
    mesh_code,
    meshes_for_bbox,
    neighbors,
    parent,
)

# ★公式 PDF の作例。千代田区・中央区あたりが 5339-46。
#   これが合わないなら式が違う。
CHIYODA_LON, CHIYODA_LAT = 139.7536, 35.6940


def test_official_example_chiyoda_is_533946():
    """★公式 PDF の作例で検算する。"""
    assert mesh_code(CHIYODA_LON, CHIYODA_LAT, 1) == "5339"
    assert mesh_code(CHIYODA_LON, CHIYODA_LAT, 2) == "533946"
    assert mesh_code(CHIYODA_LON, CHIYODA_LAT, 3)[:6] == "533946"


def test_fujisawa_mesh():
    """藤沢市役所付近。★回帰アンカー。"""
    assert mesh_code(139.4914, 35.3392, 2) == "533903"


def test_bbox_sizes_match_the_notice():
    """1次 40分×1度 / 2次 5分×7分30秒 / 3次 30秒×45秒。"""
    for code, dlat_min, dlon_min in [
        ("5339", 40.0, 60.0),
        ("533946", 5.0, 7.5),
        ("53394600", 0.5, 0.75),
    ]:
        w, s, e, n = mesh_bbox(code)
        assert (n - s) * 60 == pytest.approx(dlat_min), code
        assert (e - w) * 60 == pytest.approx(dlon_min), code


def test_second_mesh_is_about_10km():
    """およそ10km四方（★分割単位の見積もりがこの前提に乗っている）。"""
    import math

    w, s, e, n = mesh_bbox("533903")  # 藤沢
    lat_km = (n - s) * 111.32
    lon_km = (e - w) * 111.32 * math.cos(math.radians((s + n) / 2))
    assert 9.0 < lat_km < 9.5, lat_km
    assert 10.5 < lon_km < 11.5, lon_km


def test_code_and_bbox_round_trip():
    """bbox の中の点からコードを引くと元に戻ること。"""
    for code in ("5339", "533946", "53394600", "644142", "392000"):
        w, s, e, n = mesh_bbox(code)
        assert mesh_code((w + e) / 2, (s + n) / 2, len(code) // 2 - 1) == code


@pytest.mark.parametrize("level", [1, 2, 3])
def test_south_west_corner_belongs_to_the_mesh_north_east_does_not(level):
    """★半開区間。境界上の点は南西側の区画に入る。

    ここを取り違えると、区画の縁の道路が**どの区画にも入らない**か
    **二重に入る**。
    """
    code = mesh_code(139.7536, 35.6940, level)
    w, s, e, n = mesh_bbox(code)
    assert mesh_code(w, s, level) == code, "南西の角は自分のもの"
    assert mesh_code(e, n, level) != code, "北東の角は隣のもの"
    assert mesh_code(e - 1e-9, n - 1e-9, level) == code


# -----------------------------------------------------------------------------
# bbox → メッシュ一覧
# -----------------------------------------------------------------------------
def test_meshes_for_bbox_single():
    w, s, e, n = mesh_bbox("533946")
    inset = [w + LON2 * 0.1, s + LAT2 * 0.1, e - LON2 * 0.1, n - LAT2 * 0.1]
    assert meshes_for_bbox(*inset, level=2) == ["533946"]


def test_meshes_for_bbox_does_not_add_a_row_at_an_exact_edge():
    """★bbox の北東端がちょうど区画の角のとき、余分な行・列を作らない。"""
    w, s, _, _ = mesh_bbox("533946")
    got = meshes_for_bbox(w, s, w + LON2, s + LAT2, level=2)
    assert got == ["533946"], got


def test_meshes_for_bbox_two_by_two():
    w, s, _, _ = mesh_bbox("533946")
    got = meshes_for_bbox(w, s, w + LON2 * 1.5, s + LAT2 * 1.5, level=2)
    assert len(got) == 4
    assert "533946" in got and "533947" in got and "533956" in got


def test_meshes_for_bbox_crosses_the_first_mesh_boundary():
    """★1次区画をまたぐと桁が繰り上がる。数値の足し算では出せない。"""
    got = meshes_for_bbox(139.99, 35.66, 140.01, 35.68, level=2)
    firsts = {c[:4] for c in got}
    assert firsts == {"5339", "5340"}, got


# -----------------------------------------------------------------------------
# 親・隣
# -----------------------------------------------------------------------------
def test_parent():
    assert parent("53394600") == "533946"
    assert parent("533946") == "5339"
    with pytest.raises(MeshCodeError):
        parent("5339")


def test_neighbors_are_eight_and_exclude_self():
    ns = neighbors("533946")
    assert len(ns) == 8
    assert "533946" not in ns
    assert len(set(ns)) == 8


def test_neighbors_work_across_a_first_mesh_boundary():
    """★コードに 1 を足して隣を求める実装はここで落ちる。

    `533907` の東隣は `534000` であって `533908` ではない。
    """
    ns = neighbors("533907")
    assert "534000" in ns, ns
    assert not any(c.endswith("08") for c in ns)


def test_neighbors_ring_two():
    assert len(neighbors("533946", ring=2)) == 24


# -----------------------------------------------------------------------------
# 入力の型 ── ★T-16 と同じ筋の事故を止める
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [533946, "53394", "5339460", "", "５３３９４６", "5339-46", None])
def test_code_must_be_a_string_of_4_6_or_8_ascii_digits(bad):
    """★数値で渡させない。`53394600` を int にすると 0 が落ちて 6桁に見える。

    ★全角数字も弾く（Python の `\\d` は全角にも一致するので `\\d` を使わない）。
    """
    with pytest.raises(MeshCodeError):
        mesh_bbox(bad)  # type: ignore[arg-type]


def test_a_third_level_code_with_trailing_zeros_keeps_its_digits():
    code = mesh_code(CHIYODA_LON, CHIYODA_LAT, 3)
    assert len(code) == 8
    # ★数値にすると桁が落ちうることを、テストの形で残しておく
    assert mesh_bbox(code)[0] == pytest.approx(mesh_bbox(str(int(code)).zfill(8))[0])


def test_vectorized_mesh2_codes_match_scalar():
    """★配列版は1点版と1件も違わないこと（境界ちょうどの点を含めて）。"""
    import numpy as np

    from kansui.mesh import mesh2_codes, mesh_bbox

    rng = np.random.default_rng(0)
    lon = rng.uniform(122.5, 148.5, 20000)
    lat = rng.uniform(24.1, 45.5, 20000)
    # 区画の角・辺ちょうどの点
    w, s, e, n = mesh_bbox("533945")
    lon = np.r_[lon, w, e, w, e, (w + e) / 2]
    lat = np.r_[lat, s, n, n, s, s]
    got = mesh2_codes(lon, lat)
    want = [int(mesh_code(x, y, 2)) for x, y in zip(lon, lat, strict=True)]
    assert got.tolist() == want
