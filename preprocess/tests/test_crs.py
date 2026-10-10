"""平面直角座標系（19系）の引き当ての単体テスト。

★守りたいのは「静かに間違った系で距離を測る」こと。
  系を取り違えても例外は出ず、Δh の半径も幅員の断面も**もっともらしい値**のまま
  ずれるだけなので、テストで固定しないと気づけない（藤沢版 T-05 の全国版）。

一次情報: 平面直角座標系（平成十四年国土交通省告示第九号）
          最終改正 平成二十二年三月三十一日国土交通省告示第二百八十九号
          https://www.gsi.go.jp/LAW/heimencho.html （2026-09-20 取得）
"""

from __future__ import annotations

import pytest

from kansui.crs import (
    LAND_BORDER_MULTI_ZONE_PREFS,
    MULTI_ZONE_PREFS,
    N_ZONES,
    Place,
    ZoneUnknownError,
    epsg_for,
    epsg_of,
    zone_of,
    zone_of_epsg,
)


# -----------------------------------------------------------------------------
# EPSG コードとの対応
# -----------------------------------------------------------------------------
def test_epsg_range_matches_jgd2011():
    """I系=6669 … XIX系=6687。★藤沢版の 6677 が IX系であることを固定する。"""
    assert epsg_of(1) == 6669
    assert epsg_of(9) == 6677, "藤沢版が使っていた EPSG:6677 は第IX系"
    assert epsg_of(N_ZONES) == 6687
    for z in range(1, N_ZONES + 1):
        assert zone_of_epsg(epsg_of(z)) == z


def test_epsg_refuses_out_of_range():
    for bad in (0, 20, -1):
        with pytest.raises(ValueError):
            epsg_of(bad)
    for bad in (6668, 6688, 4326, 3857):
        with pytest.raises(ValueError):
            zone_of_epsg(bad)


# -----------------------------------------------------------------------------
# ★回帰アンカー: 藤沢市は IX系 / EPSG:6677
# -----------------------------------------------------------------------------
def test_fujisawa_is_zone_ix():
    """★藤沢版と同じ系が出ること。ここがずれたら全国化が壊れている。"""
    fujisawa = Place(pref_code="14", lon=139.49, lat=35.34, municipality="藤沢市")
    assert zone_of(fujisawa) == 9
    assert epsg_for(fujisawa) == 6677


# -----------------------------------------------------------------------------
# 県まるごと1系のもの
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("pref_code", "name", "zone"),
    [
        ("42", "長崎県", 1),
        ("40", "福岡県", 2), ("45", "宮崎県", 2),
        ("34", "広島県", 3),
        ("39", "高知県", 4),
        ("28", "兵庫県", 5),
        ("27", "大阪府", 6), ("24", "三重県", 6),
        ("23", "愛知県", 7),
        ("22", "静岡県", 8), ("20", "長野県", 8),
        ("11", "埼玉県", 9), ("07", "福島県", 9),
        ("04", "宮城県", 10), ("02", "青森県", 10),
    ],
)
def test_single_zone_prefectures(pref_code, name, zone):
    assert zone_of(Place(pref_code=pref_code)) == zone, name


def test_every_prefecture_code_resolves():
    """01〜47 のすべてが引けること（特殊な4県は必要な情報を添えて）。

    ★取りこぼしがあると、その県だけ静かに例外で落ちる。
    """
    special = {
        "01": Place(pref_code="01", subprefecture="石狩振興局", municipality="札幌市"),
        "13": Place(pref_code="13", lon=139.69, lat=35.69),
        "46": Place(pref_code="46", lon=130.56, lat=31.60),
        "47": Place(pref_code="47", lon=127.68, lat=26.21),
    }
    for i in range(1, 48):
        code = f"{i:02d}"
        place = special.get(code, Place(pref_code=code))
        assert 1 <= zone_of(place) <= N_ZONES, code


# -----------------------------------------------------------------------------
# 北海道 ── ★市名と振興局名の両方を見ないと引けない
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("subpref", "muni", "zone", "why"),
    [
        ("後志総合振興局", "小樽市", 11, "告示に市名が単独で挙がっている"),
        ("渡島総合振興局", "函館市", 11, "同上"),
        ("檜山振興局", "江差町", 11, "振興局まるごと XI系"),
        ("胆振総合振興局", "洞爺湖町", 11, "★胆振のうち3町だけが XI系"),
        ("胆振総合振興局", "豊浦町", 11, "同上"),
        ("胆振総合振興局", "室蘭市", 12, "★同じ胆振でも3町以外は XII系"),
        ("石狩振興局", "札幌市", 12, "XI/XIII のどちらにも入らない"),
        ("上川総合振興局", "旭川市", 12, "同上"),
        ("宗谷総合振興局", "稚内市", 12, "同上"),
        ("十勝総合振興局", "帯広市", 13, "振興局まるごと XIII系"),
        ("オホーツク総合振興局", "網走市", 13, "同上"),
        ("根室振興局", "根室市", 13, "同上"),
        ("釧路総合振興局", "釧路町", 13, "同上"),
    ],
)
def test_hokkaido_zones(subpref, muni, zone, why):
    place = Place(pref_code="01", subprefecture=subpref, municipality=muni)
    assert zone_of(place) == zone, f"{subpref} {muni}: {why}"


def test_hokkaido_accepts_the_notice_spelling_with_hokkaido_prefix():
    """告示は「北海道後志総合振興局」と『北海道』を冠している。

    ★N03 の `N03_002` は「後志総合振興局」なので、**どちらでも引ける**こと。
    """
    a = Place(pref_code="01", subprefecture="北海道後志総合振興局", municipality="倶知安町")
    b = Place(pref_code="01", subprefecture="後志総合振興局", municipality="倶知安町")
    assert zone_of(a) == zone_of(b) == 11


def test_hokkaido_refuses_to_guess():
    """★情報が足りないときに XII へ丸めない（絶対ルール10）。"""
    with pytest.raises(ZoneUnknownError):
        zone_of(Place(pref_code="01"))
    with pytest.raises(ZoneUnknownError):
        # 胆振は町名が分からないと XI/XII を決められない
        zone_of(Place(pref_code="01", subprefecture="胆振総合振興局"))
    with pytest.raises(ZoneUnknownError):
        # 振興局が分からないまま、知らない町名を XII と言い切らない
        zone_of(Place(pref_code="01", municipality="架空町"))


# -----------------------------------------------------------------------------
# 東京都・沖縄県・鹿児島県 ── 経緯度で切られる
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("lon", "lat", "zone", "where"),
    [
        (139.69, 35.69, 9, "新宿区（本土）"),
        (139.28, 34.75, 9, "★伊豆大島。北緯28度より北なので IX系"),
        (142.19, 27.09, 14, "小笠原・父島"),
        (136.08, 20.42, 18, "沖ノ鳥島"),
        (153.98, 24.29, 19, "南鳥島"),
    ],
)
def test_tokyo_zones(lon, lat, zone, where):
    assert zone_of(Place(pref_code="13", lon=lon, lat=lat)) == zone, where


@pytest.mark.parametrize(
    ("lon", "zone", "where"),
    [
        (127.68, 15, "那覇（沖縄本島）"),
        (124.15, 16, "石垣島"),
        (125.28, 16, "宮古島"),
        (131.23, 17, "南大東島"),
    ],
)
def test_okinawa_zones(lon, zone, where):
    assert zone_of(Place(pref_code="47", lon=lon, lat=26.0)) == zone, where


@pytest.mark.parametrize(
    ("lon", "lat", "amami", "zone", "where"),
    [
        (130.56, 31.60, False, 2, "鹿児島市（本土）。東経130度より東で区域外"),
        (130.99, 30.73, False, 2, "種子島。同上"),
        (129.50, 28.37, False, 1, "奄美大島・名瀬。区域内"),
        (129.20, 29.60, False, 1, "トカラ列島付近。区域内"),
        # ★東経130度〜130度13分の帯。奄美群島に限り I系に含まれる
        (130.05, 28.32, True, 1, "喜界島。奄美群島なので東端が130度13分まで延びる"),
        (130.05, 28.32, False, 2, "★同じ座標でも奄美群島でなければ II系"),
    ],
)
def test_kagoshima_zones(lon, lat, amami, zone, where):
    assert zone_of(Place(pref_code="46", lon=lon, lat=lat, amami=amami)) == zone, where


def test_lonlat_required_where_the_notice_uses_coordinates():
    """★経緯度で切られる県で lon/lat が無いとき、本土の系へ丸めない。"""
    for code in ("13", "46", "47"):
        with pytest.raises(ZoneUnknownError):
            zone_of(Place(pref_code=code))


# -----------------------------------------------------------------------------
# 入力の型 ── ★T-16（YAML の 0 始まりが8進数になる）と同じ事故を型で止める
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [1, "1", "014", "", None, "１４", "1４"])
def test_pref_code_must_be_two_ascii_digits(bad):
    """★全角数字を弾くこと。

    Python の `\\d` は全角数字（１４）にも一致するので、`\\d` で検査すると
    通ってしまう。藤沢版 T-16 で実際にこれを踏んだ。
    """
    with pytest.raises(ValueError):
        zone_of(Place(pref_code=bad))  # type: ignore[arg-type]


# -----------------------------------------------------------------------------
# ★NK-08 の範囲 ── 陸上の道路が系をまたぐのはどこか
# -----------------------------------------------------------------------------
def test_only_hokkaido_has_a_land_border_between_zones():
    """★この主張がこのモジュールの設計判断そのものなので、テストで固定する。

    告示の適用区域を読むと、北海道以外の「複数系にまたがる県」は
    分かれ目がすべて海上である（東京都の小笠原・沖ノ鳥島・南鳥島、
    鹿児島県の奄美群島等、沖縄県の宮古八重山・大東諸島）。
    → **NK-08 は実質 北海道だけの問題。**
    """
    assert set(MULTI_ZONE_PREFS) == {"01", "13", "46", "47"}
    assert LAND_BORDER_MULTI_ZONE_PREFS == {"01"}
    assert MULTI_ZONE_PREFS["01"] == (11, 12, 13)


def test_hokkaido_land_border_pairs_differ():
    """パイロット（地域2）で使う境界の候補。★系が違うことだけを固定する。

    ★「実際に道路でつながっているか」はここでは主張しない。
      隣接の確認は Phase 2 で N03（行政区域）と道路網に当たって行う。
    """
    pairs = [
        (("後志総合振興局", "小樽市"), ("石狩振興局", "札幌市")),
        (("胆振総合振興局", "伊達市"), ("胆振総合振興局", "室蘭市")),
        (("上川総合振興局", "南富良野町"), ("十勝総合振興局", "新得町")),
    ]
    for (sa, ma), (sb, mb) in pairs:
        za = zone_of(Place(pref_code="01", subprefecture=sa, municipality=ma))
        zb = zone_of(Place(pref_code="01", subprefecture=sb, municipality=mb))
        assert za != zb, f"{ma}({za}) と {mb}({zb}) は別の系であるはず"
