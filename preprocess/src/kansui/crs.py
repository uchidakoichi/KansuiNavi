"""平面直角座標系（19系）の引き当て。

藤沢版は EPSG:6677（第IX系）を 19ファイル・66行にわたって直書きしていた。
全国版では**地域から系を引き当てる**必要がある（T-05 / NK-08）。

━━ 一次情報 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  平面直角座標系（平成十四年国土交通省告示第九号）
  告示日   : 平成十四年一月十日
  最終改正 : 平成二十二年三月三十一日国土交通省告示第二百八十九号
  出典     : 国土地理院 https://www.gsi.go.jp/LAW/heimencho.html
  取得日   : 2026年9月20日
  ★適用区域の文言は、下の各定数のコメントに**告示のまま**写してある。
    要約や言い換えをしない。ここを書き換えるときは必ず告示に当たること。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

★**T-10 を全19系に一般化する。**
  EPSG:6669〜6687 の軸順は northing, easting（X=北, Y=東）で GIS 一般と逆。
  pyproj では `always_xy=True` を明示した Transformer を使うこと。
  また EPSG:6669〜6687 は 2025年4月に名称が JGD2011 → JGD2024 に変わった
  （定義・座標値は不変）。**CRS を名前で比較せず EPSG コードで比較する。**

★**この モジュールは地理空間ライブラリに依存しない。**
  経緯度と行政区域名だけで決まるので、geopandas を入れずに単体テストできる。
"""

from __future__ import annotations

from dataclasses import dataclass

# 第I系の EPSG コード。以降 1つずつ増える（I=6669 … XIX=6687）。
_EPSG_ZONE_I = 6669
N_ZONES = 19


def epsg_of(zone: int) -> int:
    """系番号（1〜19）→ EPSG コード（JGD2011 平面直角座標系）。"""
    if not 1 <= zone <= N_ZONES:
        raise ValueError(f"系番号は 1〜{N_ZONES}: {zone}")
    return _EPSG_ZONE_I + zone - 1


def zone_of_epsg(epsg: int) -> int:
    """EPSG コード → 系番号。範囲外は ValueError。"""
    zone = epsg - _EPSG_ZONE_I + 1
    if not 1 <= zone <= N_ZONES:
        raise ValueError(f"平面直角座標系の EPSG ではない: {epsg}")
    return zone


# -----------------------------------------------------------------------------
# 都道府県コード（JIS X 0401）→ 系番号
#
# ★ここに載せるのは「県まるごと1つの系」に収まるものだけ。
#   北海道・東京都・沖縄県・鹿児島県は区域が分かれるので **別扱い**（下の関数）。
# -----------------------------------------------------------------------------
_PREF_ZONE: dict[str, int] = {
    # 第I系  : 長崎県（鹿児島県の島嶼は _kagoshima_zone で判定）
    "42": 1,
    # 第II系 : 福岡県、佐賀県、熊本県、大分県、宮崎県、鹿児島県（I系の区域を除く）
    "40": 2, "41": 2, "43": 2, "44": 2, "45": 2,
    # 第III系: 山口県、島根県、広島県
    "35": 3, "32": 3, "34": 3,
    # 第IV系 : 香川県、愛媛県、徳島県、高知県
    "37": 4, "38": 4, "36": 4, "39": 4,
    # 第V系  : 兵庫県、鳥取県、岡山県
    "28": 5, "31": 5, "33": 5,
    # 第VI系 : 京都府、大阪府、福井県、滋賀県、三重県、奈良県、和歌山県
    "26": 6, "27": 6, "18": 6, "25": 6, "24": 6, "29": 6, "30": 6,
    # 第VII系: 石川県、富山県、岐阜県、愛知県
    "17": 7, "16": 7, "21": 7, "23": 7,
    # 第VIII系: 新潟県、長野県、山梨県、静岡県
    "15": 8, "20": 8, "19": 8, "22": 8,
    # 第IX系 : 東京都（XIV系、XVIII系及びXIX系に規定する区域を除く）、福島県、
    #          栃木県、茨城県、埼玉県、千葉県、群馬県、神奈川県
    #          ★東京都(13) は _tokyo_zone で判定する
    "07": 9, "09": 9, "08": 9, "11": 9, "12": 9, "10": 9, "14": 9,
    # 第X系  : 青森県、秋田県、山形県、岩手県、宮城県
    "02": 10, "05": 10, "06": 10, "03": 10, "04": 10,
}

# -----------------------------------------------------------------------------
# 北海道（第XI系・第XIII系）── 告示は**振興局名と市名**で区域を定める
#
# 告示 第XI系:
#   「小樽市、函館市、伊達市、北斗市、北海道後志総合振興局、
#     北海道胆振総合振興局（豊浦町、壮瞥町及び洞爺湖町）、
#     北海道渡島総合振興局、北海道檜山振興局」
# 告示 第XIII系:
#   「北見市、帯広市、釧路市、網走市、根室市、北海道オホーツク総合振興局、
#     北海道十勝総合振興局、北海道釧路総合振興局、北海道根室振興局」
# 告示 第XII系:
#   「北海道（XI系及びXIII系に規定する区域を除く）」
#
# ★**市が単独で名指しされている**のは、これらの市が振興局の所管から外れる
#   （振興局所在地等）ため。市名と振興局名の**両方**を見ないと正しく引けない。
# ★振興局の名称は 2010年（平成22年）の支庁再編後のもの。告示の最終改正が
#   平成22年3月31日なのはこの再編に対応したものである。
# -----------------------------------------------------------------------------
_HOKKAIDO_XI_CITIES = frozenset({"小樽市", "函館市", "伊達市", "北斗市"})
_HOKKAIDO_XI_SUBPREF = frozenset({"後志総合振興局", "渡島総合振興局", "檜山振興局"})
# ★胆振総合振興局は**全域ではない**。この3町だけが XI系。
_HOKKAIDO_XI_IBURI_TOWNS = frozenset({"豊浦町", "壮瞥町", "洞爺湖町"})

_HOKKAIDO_XIII_CITIES = frozenset({"北見市", "帯広市", "釧路市", "網走市", "根室市"})
_HOKKAIDO_XIII_SUBPREF = frozenset({
    "オホーツク総合振興局", "十勝総合振興局", "釧路総合振興局", "根室振興局",
})


class ZoneUnknownError(ValueError):
    """系を決めるのに要る情報が足りない。★推測で埋めないための例外（絶対ルール10）。"""


def _normalize_subpref(name: str | None) -> str | None:
    """N03 の支庁名の表記ゆれを吸収する。

    ★N03（行政区域データ）の `N03_002` は「石狩振興局」のように入るが、
      告示は「北海道後志総合振興局」と『北海道』を冠している。
      ★**勝手に他の言い換えまで許さない。** 頭の「北海道」を外すだけ。
    """
    if name is None:
        return None
    return name.removeprefix("北海道").strip() or None


def _hokkaido_zone(subprefecture: str | None, municipality: str | None) -> int:
    sub = _normalize_subpref(subprefecture)
    muni = (municipality or "").strip() or None
    if sub is None and muni is None:
        raise ZoneUnknownError(
            "北海道は XI/XII/XIII 系に分かれる。振興局名か市町村名が要る"
        )
    if muni in _HOKKAIDO_XI_CITIES:
        return 11
    if muni in _HOKKAIDO_XIII_CITIES:
        return 13
    if sub in _HOKKAIDO_XI_SUBPREF:
        return 11
    if sub in _HOKKAIDO_XIII_SUBPREF:
        return 13
    if sub == "胆振総合振興局":
        # ★3町だけが XI系。町名が分からないと決められない。
        if muni is None:
            raise ZoneUnknownError("胆振総合振興局は町によって XI/XII が分かれる")
        return 11 if muni in _HOKKAIDO_XI_IBURI_TOWNS else 12
    if sub is None:
        # 市町村名だけ渡され、上のどれにも当たらなかった。
        # ★振興局が分からないと XII と言い切れない（市名が XI/XIII 側の町村かもしれない）。
        raise ZoneUnknownError(f"振興局名が無いと系を決められない: {muni}")
    return 12


# -----------------------------------------------------------------------------
# 経緯度で区域が切られる系（東京都・沖縄県・鹿児島県）
#
# ★度分秒をそのまま書く。10進度に直した数値だけを置かない
#   （告示の値と突き合わせられなくなるため）。
# -----------------------------------------------------------------------------
def _dms(deg: int, minute: int = 0, sec: float = 0.0) -> float:
    return deg + minute / 60.0 + sec / 3600.0


_LAT_28 = _dms(28)             # 東京都の島嶼を分ける北緯28度
_LON_140_30 = _dms(140, 30)    # 東経140度30分
_LON_143 = _dms(143)           # 東経143度
_LON_126 = _dms(126)           # 沖縄県 XVI/XV の境
_LON_130 = _dms(130)           # 沖縄県 XV/XVII の境、鹿児島 I系の東端
_LAT_27 = _dms(27)             # 鹿児島 I系の南端
_LAT_32 = _dms(32)             # 鹿児島 I系の北端
_LON_128_18 = _dms(128, 18)    # 鹿児島 I系の西端
_LON_130_13 = _dms(130, 13)    # ★奄美群島に限り東端がここまで延びる


def _tokyo_zone(lon: float, lat: float) -> int:
    """東京都。

    告示:
      第XIV系  「東京都のうち北緯28度から南であり、かつ東経140度30分から東であり
                 東経143度から西である区域」（小笠原）
      第XVIII系「東京都のうち北緯28度から南であり、かつ東経140度30分から西である区域」
      第XIX系  「東京都のうち北緯28度から南であり、かつ東経143度から東である区域」
      第IX系   「東京都（XIV系、XVIII系及びXIX系に規定する区域を除く）」

    ★北緯28度以北（本土・伊豆諸島）は IX系。
    """
    if lat >= _LAT_28:
        return 9
    if lon < _LON_140_30:
        return 18
    if lon > _LON_143:
        return 19
    return 14


def _okinawa_zone(lon: float) -> int:
    """沖縄県。

    告示:
      第XV系  「沖縄県のうち東経126度から東であり、かつ東経130度から西である区域」
      第XVI系 「沖縄県のうち東経126度から西である区域」
      第XVII系「沖縄県のうち東経130度から東である区域」
    """
    if lon < _LON_126:
        return 16
    if lon > _LON_130:
        return 17
    return 15


def _kagoshima_zone(lon: float, lat: float, amami: bool) -> int:
    """鹿児島県。

    告示 第I系:
      「長崎県、鹿児島県のうち北方北緯32度南方北緯27度西方東経128度18分
        東方東経130度を境界線とする区域内（奄美群島は東経130度13分までを含む）
        にあるすべての島、小島、環礁及び岩礁」
    告示 第II系:
      「…鹿児島県（I系に規定する区域を除く）」

    ★`amami` は「その地点が奄美群島か」。**経緯度だけでは決まらない**ので
      呼び出し側が渡す。分からないまま東端を 130度13分 に広げない。
    """
    east = _LON_130_13 if amami else _LON_130
    in_box = (_LAT_27 <= lat <= _LAT_32) and (_LON_128_18 <= lon <= east)
    return 1 if in_box else 2


# -----------------------------------------------------------------------------
# 入口
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Place:
    """系を引き当てるのに要る最小限の情報。

    pref_code    : 都道府県コード（JIS X 0401。**2桁の文字列**）
                   ★"01" を int 1 で渡さない。T-16（YAML の 0 始まりは8進数）と
                     同じ事故を、型で止める。
    lon, lat     : 経緯度（EPSG:6668/4326）。区域が経緯度で切られる県に要る
    subprefecture: 北海道の振興局名（N03_002）
    municipality : 市町村名（N03_004）
    amami        : ★その地点が奄美群島か。鹿児島県でのみ意味を持つ
    """

    pref_code: str
    lon: float | None = None
    lat: float | None = None
    subprefecture: str | None = None
    municipality: str | None = None
    amami: bool = False


def zone_of(place: Place) -> int:
    """その地点の平面直角座標系の系番号（1〜19）を返す。

    ★決められないときは `ZoneUnknownError` を投げる。既定値に落とさない。
    """
    code = place.pref_code
    if not isinstance(code, str) or len(code) != 2 or not code.isascii() \
            or not all(c in "0123456789" for c in code):
        # ★`\d` を使わない。Python の `\d` は全角数字にも一致する（藤沢版 T-16）。
        raise ValueError(f"都道府県コードは半角2桁の文字列: {code!r}")

    if code == "01":  # 北海道
        return _hokkaido_zone(place.subprefecture, place.municipality)

    if code in ("13", "46", "47"):  # 東京都・鹿児島県・沖縄県は経緯度が要る
        if place.lon is None or place.lat is None:
            raise ZoneUnknownError(
                f"都道府県コード {code} は区域が経緯度で切られる。lon/lat が要る"
            )
        if code == "13":
            return _tokyo_zone(place.lon, place.lat)
        if code == "47":
            return _okinawa_zone(place.lon)
        return _kagoshima_zone(place.lon, place.lat, place.amami)

    try:
        return _PREF_ZONE[code]
    except KeyError:
        raise ValueError(f"都道府県コードが範囲外: {code!r}") from None


def epsg_for(place: Place) -> int:
    """その地点で距離計算に使うべき EPSG コード。"""
    return epsg_of(zone_of(place))


# -----------------------------------------------------------------------------
# ★陸続きで系の境界をまたぐのはどこか（NK-08 の範囲を絞るための整理）
# -----------------------------------------------------------------------------
#: 複数の系にまたがる都道府県と、その系。
#: ★「またがる」＝区域が分かれること。**陸上の道路がまたぐか**は別問題。
MULTI_ZONE_PREFS: dict[str, tuple[int, ...]] = {
    "01": (11, 12, 13),   # 北海道 ── ★陸続きで分かれる
    "13": (9, 14, 18, 19),  # 東京都 ── IX系以外は小笠原・沖ノ鳥島・南鳥島
    "46": (1, 2),         # 鹿児島県 ── I系は奄美群島等の島嶼のみ
    "47": (15, 16, 17),   # 沖縄県 ── 島ごとに分かれる
}

#: ★**陸上の道路が系の境界をまたぎうる**都道府県。
#:
#: 告示の適用区域を読むと、北海道以外の「またがる県」は
#: **分かれ目がすべて海上**である:
#:   東京都  … IX系（本土・伊豆諸島）以外は小笠原・沖ノ鳥島・南鳥島
#:   鹿児島県 … I系は奄美群島・トカラ列島等の島嶼のみ（本土は II系）
#:   沖縄県  … XV系（沖縄本島等）/ XVI系（宮古・八重山）/ XVII系（大東諸島）
#:
#: → **道路が系をまたぐ問題（NK-08）は、実質 北海道だけの問題である。**
#:   例: 小樽市(XI系) ↔ 札幌市(XII系) は隣接し、国道で結ばれている。
#:       伊達市(XI系) ↔ 室蘭市(XII系) も隣接する。
#:       上川総合振興局(XII系) ↔ 十勝総合振興局(XIII系) も陸続き。
LAND_BORDER_MULTI_ZONE_PREFS: frozenset[str] = frozenset({"01"})
