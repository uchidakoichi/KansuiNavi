"""標高タイルの列挙・フォールバックのテスト（通信しない）。

★守りたいのは「静かに間違った標高を使う」こと:
  - T-08: `− 2^24` を落とすと海面下が巨大な正値になり冠水判定が根本から壊れる
  - T-09: RGB(128,0,0) は「欠損」であって「標高0m」ではない
  - ★種別ごとにズームが違うのに、同じ x/y で下位種別を叩くと**別の場所**を取る
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from kansui.dem import (
    DemSource,
    fallback_chain,
    fetch_tile_with_fallback,
    lonlat_to_tile,
    tile_bounds,
    tiles_for_polygon,
)
from kansui.dem_png import decode_dem_png
from kansui.fetcher import NOT_FOUND

SOURCES = [
    {"name": "dem1a", "zoom": 17, "sd_m": 0.3, "url": "https://x/dem1a/{z}/{x}/{y}.png"},
    {"name": "dem5a", "zoom": 15, "sd_m": 0.3, "url": "https://x/dem5a/{z}/{x}/{y}.png"},
    {"name": "dem5b", "zoom": 15, "sd_m": 0.7, "url": "https://x/dem5b/{z}/{x}/{y}.png"},
    {"name": "dem10b", "zoom": 14, "sd_m": 5.0, "url": "https://x/dem10b/{z}/{x}/{y}.png"},
]


def _png(rgb: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), rgb).save(buf, format="PNG")
    return buf.getvalue()


# -----------------------------------------------------------------------------
# 復号（T-08 / T-09）
# -----------------------------------------------------------------------------
def test_negative_elevation_decodes_below_sea_level():
    """★T-08: `− 2^24` を落とすと海面下が巨大な正値になる。

    d = 2^24 − 100 → h = −1.00m。落とすと +167772.16m になる。
    """
    d = (1 << 24) - 100
    rgb = ((d >> 16) & 255, (d >> 8) & 255, d & 255)
    h = decode_dem_png(_png(rgb))
    assert h[0, 0] == pytest.approx(-1.0, abs=1e-4), h[0, 0]
    assert h.min() < 0, "★海面下が負で出ること"


def test_nodata_is_nan_not_zero():
    """★T-09: RGB(128,0,0) は欠損。0m と誤読すると最も危険と誤判定される。"""
    h = decode_dem_png(_png((128, 0, 0)))
    assert np.isnan(h).all()
    assert not (h == 0).any()


def test_zero_elevation_is_really_zero():
    """RGB(0,0,0) は本当に 0m。欠損と混同しない。"""
    h = decode_dem_png(_png((0, 0, 0)))
    assert (h == 0).all()
    assert not np.isnan(h).any()


# -----------------------------------------------------------------------------
# フォールバックの順番
# -----------------------------------------------------------------------------
def test_chain_starts_at_primary_and_skips_higher_resolution():
    """★DEM1A は14倍のコストに対し Δh の差が 0.15% だったので使わない。"""
    chain = fallback_chain(SOURCES, "dem5a")
    assert [s.name for s in chain] == ["dem5a", "dem5b", "dem10b"]
    assert "dem1a" not in [s.name for s in chain]


def test_chain_refuses_an_unknown_primary():
    with pytest.raises(ValueError, match="dem.primary"):
        fallback_chain(SOURCES, "dem9z")


class _FakeFetcher:
    """欲しい種別だけ返す。★どの URL を叩いたかを記録する。"""

    def __init__(self, serve: dict[str, tuple[int, int, int]]):
        self.serve = serve
        self.urls: list[str] = []

    def fetch(self, url: str):
        self.urls.append(url)
        for name, rgb in self.serve.items():
            if f"/{name}/" in url:
                return _png(rgb)
        return NOT_FOUND


def test_falls_back_when_the_primary_is_missing():
    f = _FakeFetcher({"dem5b": (0, 100, 0)})
    name, arr = fetch_tile_with_fallback(f, fallback_chain(SOURCES, "dem5a"), 29069, 12938, 15)
    assert name == "dem5b"
    assert arr is not None and not np.isnan(arr).any()


def test_falls_back_when_the_primary_is_entirely_nodata():
    """★全画素が欠損なら「取れた」と見なさない（T-09）。"""
    f = _FakeFetcher({"dem5a": (128, 0, 0), "dem5b": (0, 50, 0)})
    name, _ = fetch_tile_with_fallback(f, fallback_chain(SOURCES, "dem5a"), 29069, 12938, 15)
    assert name == "dem5b", "全欠損の dem5a を採用してはいけない"


def test_missing_everywhere_returns_none_not_zero():
    """★どの種別でも取れなければ「標高不明」。0m で埋めない。"""
    f = _FakeFetcher({})
    name, arr = fetch_tile_with_fallback(f, fallback_chain(SOURCES, "dem5a"), 29069, 12938, 15)
    assert name is None and arr is None


def test_tile_number_is_recomputed_for_a_different_zoom():
    """★★同じ x/y を別のズームで叩くと**まったく別の場所**になる。

    dem5a(z15) の 29069/12938 と、dem10b(z14) の同じ番号は別の土地。
    フォールバックのときに番号を引き直していることを固定する。
    """
    f = _FakeFetcher({"dem10b": (0, 10, 0)})
    x, y, z = 29069, 12938, 15
    name, _ = fetch_tile_with_fallback(f, fallback_chain(SOURCES, "dem5a"), x, y, z)
    assert name == "dem10b"
    url = next(u for u in f.urls if "/dem10b/" in u)
    assert f"/14/{x}/{y}.png" not in url, "★z15 の番号をそのまま z14 で叩いている"
    # 中心の経緯度から引き直した番号であること
    w, s, e, n = tile_bounds(x, y, z)
    ex, ey = lonlat_to_tile((w + e) / 2, (s + n) / 2, 14)
    assert url.endswith(f"/14/{ex}/{ey}.png"), url


# -----------------------------------------------------------------------------
# 列挙 ── ★bbox ではなくポリゴンに重なるもの
# -----------------------------------------------------------------------------
def test_tiles_for_polygon_skips_tiles_outside_the_shape():
    """★神奈川県では bbox 6,030枚に対し実際は 3,620枚だった。

    bbox で列挙すると海と隣県を 2,410枚ぶん無駄に叩くことになる。
    """
    from shapely.geometry import Polygon

    # 細い対角の帯。bbox は広いが実際に重なるタイルは少ない
    band = Polygon([(139.40, 35.30), (139.41, 35.30), (139.60, 35.50), (139.59, 35.50)])
    z = 15
    got = tiles_for_polygon(band, z)
    w, s, e, n = band.bounds
    x0, y0 = lonlat_to_tile(w, n, z)
    x1, y1 = lonlat_to_tile(e, s, z)
    bbox_n = (x1 - x0 + 1) * (y1 - y0 + 1)
    assert 0 < len(got) < bbox_n, (len(got), bbox_n)
    assert len(set(got)) == len(got), "同じタイルを二度並べない"


def test_tile_bounds_round_trip():
    z = 15
    x, y = lonlat_to_tile(139.4914, 35.3392, z)
    w, s, e, n = tile_bounds(x, y, z)
    assert w < 139.4914 < e and s < 35.3392 < n
    assert lonlat_to_tile((w + e) / 2, (s + n) / 2, z) == (x, y)


def test_dem_source_from_config_keeps_the_zoom_per_type():
    """★種別ごとに叩くズームが違う。取り違えると別の場所を取る。"""
    src = {s.name: s for s in [DemSource.from_config(d) for d in SOURCES]}
    assert src["dem5a"].zoom == 15
    assert src["dem10b"].zoom == 14
    assert src["dem1a"].zoom == 17


def test_network_failure_is_not_treated_as_missing():
    """★通信の失敗（None）で次の種別へ落ちない。障害が「データが無い」に化けて焼き付くため。"""
    import pytest

    from kansui.dem import DemFetchError

    class Down:
        def fetch(self, url):
            return None

    chain = [DemSource("dem5a", 15, 0.3, "https://example/{z}/{x}/{y}.png")]
    with pytest.raises(DemFetchError):
        fetch_tile_with_fallback(Down(), chain, 29099, 12851, 15)
