"""前処理を流す「地域」の定義と、そこから決まるもの。

★全国版の中核。**対象地域は引数**であり、コードに書かない。
  藤沢版は団体コード `14205` と `EPSG:6677` を直書きしていた。

この モジュールが決めるもの:
  1. どの行政区域を処理するか（N03 の絞り込み）
  2. その区域が**どの平面直角座標系**か（`kansui.crs`）── ★1地域が複数系にまたがりうる
  3. どの**メッシュ区画**に分割するか（`kansui.mesh`）
  4. ハザードタイルの URL（都道府県別レイヤ）

★**N03（行政区域データ）は引数で受け取る。** ここでダウンロードしない。
  そのおかげでこの モジュールは実データ無しで単体テストできる。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from kansui.crs import Place, ZoneUnknownError, epsg_of, zone_of
from kansui.mesh import mesh_code, meshes_for_bbox

if TYPE_CHECKING:  # pragma: no cover
    import geopandas as gpd

# N03（国土数値情報 行政区域）の列名。
#   ★列名で引く。位置で引かない（年度で並びが変わりうる）
N03_PREF = "N03_001"       # 都道府県名
N03_SUBPREF = "N03_002"    # 支庁名・振興局名（★北海道でのみ入る）
N03_GUN = "N03_003"        # 郡名・政令指定都市名
N03_CITY = "N03_004"       # 市区町村名
N03_CODE = "N03_007"       # 行政区域コード（5桁。先頭2桁が都道府県コード）

#: ハザードタイル（重ねるハザードマップ）。`{pref}` は都道府県コード2桁。
#: ★**全国合成レイヤの有無と凡例は未確認**（NK-14）。都道府県別のまま置く。
HAZARD_TILE_URLS: dict[str, str] = {
    "flood_l2": (
        "https://disaportaldata.gsi.go.jp/raster/"
        "01_flood_l2_shinsuishin_pref_data/{pref}/{z}/{x}/{y}.png"
    ),
    "naisui": (
        "https://disaportaldata.gsi.go.jp/raster/"
        "02_naisui_pref_data/{pref}/{z}/{x}/{y}.png"
    ),
    "hightide": (
        "https://disaportaldata.gsi.go.jp/raster/"
        "03_hightide_l2_shinsuishin_pref_data/{pref}/{z}/{x}/{y}.png"
    ),
}


class RegionError(ValueError):
    """地域の指定が解決できない。★黙って空で通さない。"""


@dataclass(frozen=True)
class Region:
    """前処理を1回流す単位。

    ★`config.yaml` の `region:` をそのまま写したもの。ここを差し替えれば
      別の地域を流せる。コードは触らない（絶対ルール4）。

    pref_codes         : 都道府県コード（2桁の文字列）。必須
    municipality_codes : 5桁の団体コードで更に絞る場合。None なら県全域
    exclude_municipality_codes : 5桁の団体コードで**外す**場合（★東京都の島しょ部など）。
                         本土から数百km離れた島を含めると、区画・DEM・幅員タイルが
                         **そこまで飛び地で広がる**うえ、本土と道路でつながらない。
    mesh_codes         : メッシュを直接指定する場合（★北海道の「系の境界の帯」など）
    buffer_km          : 経路確保のための外側バッファ。藤沢版は 3.0
    """

    name: str
    pref_codes: tuple[str, ...]
    municipality_codes: tuple[str, ...] | None = None
    mesh_codes: tuple[str, ...] | None = None
    buffer_km: float = 3.0
    exclude_municipality_codes: tuple[str, ...] | None = None
    #: 画面に出す名前（例「埼玉県」）。無ければ name
    label: str | None = None

    def __post_init__(self) -> None:
        if not self.pref_codes:
            raise RegionError("pref_codes が空")
        for c in self.pref_codes:
            _check_code(c, 2, "都道府県コード")
        for c in self.municipality_codes or ():
            _check_code(c, 5, "団体コード")
            if c[:2] not in self.pref_codes:
                raise RegionError(f"団体コード {c} が pref_codes {self.pref_codes} の外")
        for c in self.exclude_municipality_codes or ():
            _check_code(c, 5, "除外する団体コード")
            # ★対象外の県のコードを「外す」と書いても何も起きない。
            #   書き間違い（13 と 14 の取り違え等）を黙って通さない
            if c[:2] not in self.pref_codes:
                raise RegionError(f"除外する団体コード {c} が pref_codes {self.pref_codes} の外")

    @classmethod
    def from_config(cls, cfg: dict) -> Region:
        """`config.yaml` の `region:` から作る。"""
        def tup(v):
            return tuple(v) if v else None
        return cls(
            name=str(cfg["name"]),
            pref_codes=tuple(cfg["pref_codes"]),
            municipality_codes=tup(cfg.get("municipality_codes")),
            mesh_codes=tup(cfg.get("mesh_codes")),
            buffer_km=float(cfg.get("buffer_km", 3.0)),
            exclude_municipality_codes=tup(cfg.get("exclude_municipality_codes")),
            label=str(cfg["label"]) if cfg.get("label") else None,
        )

    @classmethod
    def from_env(cls, cfg) -> Region:
        """環境変数 `KANSUI_REGION` で `config.yaml` の `regions:` から1つ選ぶ。

        ★**既定の地域を持たない。** 名前が無ければ落とす。
          地域を取り違えたまま数時間の取得を流すと、止めるまで気づけない。
        """
        import os

        regions = cfg.require("regions")
        name = os.environ.get("KANSUI_REGION", "").strip()
        if not name:
            raise RegionError(
                f"KANSUI_REGION で地域名を指定すること（候補: {', '.join(regions)}）"
            )
        if name not in regions:
            raise RegionError(f"地域 {name!r} は regions に無い（候補: {', '.join(regions)}）")
        return cls.from_config({**regions[name], "name": name})


def _check_code(code: object, digits: int, what: str) -> None:
    """★半角数字の文字列であることを型で要求する。

    T-16（YAML の `0466251111` が8進数として 81351241 になる）と同じ筋。
    `01`（北海道）や `01100`（札幌市中央区）は**数値にすると先頭の 0 が落ちる**。
    ★`\\d` を使わない。Python の `\\d` は全角数字にも一致する。
    """
    if not isinstance(code, str):
        raise RegionError(f"{what}は文字列: {code!r}（数値にすると先頭の 0 が落ちる）")
    if len(code) != digits or not all(c in "0123456789" for c in code):
        raise RegionError(f"{what}は半角{digits}桁: {code!r}")


# -----------------------------------------------------------------------------
# N03 の絞り込み
# -----------------------------------------------------------------------------
def select(n03: gpd.GeoDataFrame, region: Region) -> gpd.GeoDataFrame:
    """N03 から対象地域の行を取り出す。

    ★**緯度経度で切らない。** 団体コードで切る（藤沢版から継承）。
    ★1行も残らなければ例外。空のまま先へ進めない。
    """
    code = n03[N03_CODE].astype(str)
    keep = code.str[:2].isin(region.pref_codes)
    if region.municipality_codes:
        keep &= code.isin(region.municipality_codes)
    if region.exclude_municipality_codes:
        # ★外すと書いたコードが**データに1つも無い**なら、書き間違いを疑って止める。
        #   黙って何も外さずに進むと、島しょ部が入ったまま数時間の取得が走る
        missing = set(region.exclude_municipality_codes) - set(code)
        if missing:
            raise RegionError(f"除外するはずの団体コードが N03 に無い: {sorted(missing)}")
        keep &= ~code.isin(region.exclude_municipality_codes)
    out = n03.loc[keep]
    if out.empty:
        raise RegionError(f"地域 {region.name} に当たる行政区域が N03 に無い")
    return out


def places(n03_selected: gpd.GeoDataFrame) -> list[Place]:
    """N03 の行 → 系の引き当てに渡す `Place` の列。

    ★代表点は行政区域ポリゴンの**内部の点**（重心ではない）。
      重心は C 字型の区域で区域の外に落ちうる。
    """
    out = []
    geo = n03_selected.to_crs(4326) if n03_selected.crs and \
        n03_selected.crs.to_epsg() != 4326 else n03_selected
    for _, r in geo.iterrows():
        p = r.geometry.representative_point()
        out.append(Place(
            pref_code=str(r[N03_CODE])[:2],
            lon=float(p.x), lat=float(p.y),
            subprefecture=(str(r[N03_SUBPREF]) if r.get(N03_SUBPREF) else None),
            municipality=(str(r[N03_CITY]) if r.get(N03_CITY) else None),
            amami=_is_amami(r),
        ))
    return out


def _is_amami(row) -> bool:
    """★奄美群島か。奄美群島振興開発特別措置法 第一条:
    「奄美群島（鹿児島県奄美市及び大島郡の区域をいう。）」
    （e-Gov 法令ID 329AC1000000189・2025-06-01 施行版で確認。2026-09-23）

    告示の I系の東端は「奄美群島は東経130度13分までを含む」ので、これが無いと
    喜界町の東端が II系と判定される（2026-09-23 のレビューで指摘）。
    """
    if not str(row[N03_CODE]).startswith("46"):
        return False
    return row.get(N03_CITY) == "奄美市" or row.get(N03_GUN) == "大島郡"


def zones(n03_selected: gpd.GeoDataFrame) -> dict[str, int]:
    """団体コード → 系番号。

    ★決められない区域があれば `ZoneUnknownError` がそのまま上がる。
      既定の系に丸めない（絶対ルール10）。
    """
    # ★N03 の1行は島や飛び地1つ分。1つの団体に複数の行がある。
    #   ★★**最後の行の系で上書きしない**（2026-09-23 のレビューで指摘: 薩摩川内市は
    #     本土が II系・甑島が I系で、最後の行の I系に黙って寄っていた）。
    #   → 行ごとに系を引き、**面積の多い系**をその団体の系にする。またがる団体は報告する。
    import logging

    geo = n03_selected.to_crs(6933) if n03_selected.crs else n03_selected   # 正積図法で面積
    area: dict[str, dict[int, float]] = {}
    for place, code, g in zip(places(n03_selected), n03_selected[N03_CODE].astype(str),
                              geo.geometry, strict=True):
        z = zone_of(place)
        d = area.setdefault(code, {})
        d[z] = d.get(z, 0.0) + float(g.area)
    out: dict[str, int] = {}
    for code, d in area.items():
        out[code] = max(d, key=d.get)
        if len(d) > 1:
            tot = sum(d.values())
            logging.getLogger(__name__).warning(
                "★団体 %s は複数の系にまたがる: %s → 面積の多い %d 系にする", code,
                {z: f"{a / tot:.1%}" for z, a in sorted(d.items())}, out[code])
    return out


def region_epsg(n03_selected: gpd.GeoDataFrame) -> int:
    """地域全体で1つの系に収まるならその EPSG。収まらなければ例外。

    ★北海道はここで必ず落ちる（XI/XII/XIII にまたがるため）。
      **落ちるのが正しい。** 区画ごとに系を決める必要があることの合図である（NK-08）。
    """
    zs = set(zones(n03_selected).values())
    if len(zs) != 1:
        raise ZoneUnknownError(
            f"地域が複数の平面直角座標系にまたがる: {sorted(zs)}。"
            "区画ごとに系を決めること（NK-08）"
        )
    return epsg_of(zs.pop())


# -----------------------------------------------------------------------------
# メッシュへの分割
# -----------------------------------------------------------------------------
def region_meshes(
    n03_selected: gpd.GeoDataFrame, level: int = 2, buffer_km: float = 0.0
) -> list[str]:
    """地域を覆うメッシュコードの一覧。

    ★bbox ではなく**区域ポリゴンに実際に重なる**ものだけを返す。
      bbox だと海や隣県を大量に含む（藤沢では bbox 720枚 → 実際 353枚）。
    """
    from shapely.geometry import box

    g = n03_selected.to_crs(4326)
    poly = g.union_all()
    if buffer_km:
        # ★度で膨らませない。メートルで膨らませてから戻す
        import geopandas as gpd_

        m = gpd_.GeoSeries([poly], crs=4326).to_crs(3857)
        poly = m.buffer(buffer_km * 1000.0).to_crs(4326).iloc[0]
    w, s, e, n = poly.bounds
    return [c for c in meshes_for_bbox(w, s, e, n, level)
            if box(*_reorder(c)).intersects(poly)]


def _reorder(code: str) -> tuple[float, float, float, float]:
    from kansui.mesh import mesh_bbox
    w, s, e, n = mesh_bbox(code)
    return w, s, e, n


def mesh_zones(
    n03_selected: gpd.GeoDataFrame, level: int = 2
) -> dict[str, set[int]]:
    """メッシュコード → そのメッシュに重なる区域の系（複数ありうる）。

    ★**またぐメッシュを見つけるためのもの。** 値の集合が2つ以上なら、
      その区画は系の境界をまたいでいる（NK-08）。
      藤沢版の感覚では起きないが、北海道では必ず起きる。
    """
    from shapely.geometry import box

    g = n03_selected.to_crs(4326)
    z = zones(g)
    out: dict[str, set[int]] = {}
    for code in region_meshes(g, level):
        b = box(*_reorder(code))
        hit = g[g.geometry.intersects(b)]
        out[code] = {z[str(c)] for c in hit[N03_CODE].astype(str)}
    return out


def mesh_epsg(mesh: str, zs: set[int]) -> int:
    """1つの区画で使う EPSG を決める。

    ★またいでいる場合は**区画の中心が属する系**を使う…とは決めない。
      **ここは NK-08 の未決事項なので、またいでいたら例外にする。**
      Phase 2（北海道）で測ってから方針を決める。推測で埋めない。
    """
    if not zs:
        raise ZoneUnknownError(f"区画 {mesh} に重なる区域が無い")
    if len(zs) > 1:
        raise ZoneUnknownError(
            f"区画 {mesh} が複数の系にまたがる: {sorted(zs)}。"
            "扱いは NK-08 として未決。Phase 2 で決める"
        )
    return epsg_of(next(iter(zs)))


# -----------------------------------------------------------------------------
# ハザードタイル
# -----------------------------------------------------------------------------
def hazard_url(layer: str, pref_code: str) -> str:
    """都道府県別ハザードレイヤの URL テンプレート（`{z}/{x}/{y}` は残す）。"""
    _check_code(pref_code, 2, "都道府県コード")
    try:
        return HAZARD_TILE_URLS[layer].replace("{pref}", pref_code)
    except KeyError:
        raise RegionError(
            f"知らないハザードレイヤ: {layer}（{sorted(HAZARD_TILE_URLS)}）"
        ) from None


def zone_of_point(lon: float, lat: float, pref_code: str, **kw) -> int:
    """1点の系。`kansui.crs` への薄い橋渡し。"""
    return zone_of(Place(pref_code=pref_code, lon=lon, lat=lat, **kw))


def mesh_of_point(lon: float, lat: float, level: int = 2) -> str:
    """1点が属する区画。`kansui.mesh` への薄い橋渡し。"""
    return mesh_code(lon, lat, level)
