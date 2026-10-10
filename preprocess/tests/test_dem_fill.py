"""標高タイルを画素単位で下位の種別から埋める（2026-09-29）。"""

from __future__ import annotations

import numpy as np
import pytest

from kansui import dem as D
from kansui.fetcher import NOT_FOUND


def _chain():
    return [D.DemSource("dem5a", 15, 0.3, "A/{z}/{x}/{y}"), D.DemSource("dem5b", 15, 0.7, "B/{z}/{x}/{y}"),
            D.DemSource("dem10b", 14, 5.0, "T/{z}/{x}/{y}")]


class _F:
    def __init__(self, table):
        self.table, self.urls = table, []

    def fetch(self, url):
        self.urls.append(url)
        return self.table.get(url.split("/")[0], NOT_FOUND)


@pytest.fixture
def fake_decode(monkeypatch):
    monkeypatch.setattr(D, "decode_dem_png", lambda a: a)


def test_same_zoom_holes_are_filled_pixelwise(fake_decode):
    a = np.full((256, 256), 10.0, np.float32)
    a[:, :100] = np.nan                       # 5A の穴
    b = np.full((256, 256), 20.0, np.float32)
    b[:, :50] = np.nan                        # 5B でも一部は穴
    t = np.full((256, 256), 30.0, np.float32)
    f = _F({"A": a, "B": b, "T": t})
    arr, low = D.fetch_tile_filled(f, _chain(), 10, 20, 15)
    assert (arr[:, 100:] == 10).all()         # ★値のある画素は上位の種別のまま
    assert (arr[:, 50:100] == 20).all()
    # ★★DEM5 が一部でもあるタイルでは DEM10B で埋めない（水面を埋めない。7回目のレビュー）
    assert np.isnan(arr[:, :50]).all() and not low.any()
    assert not any(u.startswith("T") for u in f.urls)   # 取りにも行かない


def test_river_hole_is_not_filled_from_dem10b(fake_decode):
    """★DEM5A の川（内側の欠損）を DEM10B で埋めない。"""
    a = np.full((256, 256), 10.0, np.float32)
    a[100:120, :] = np.nan
    arr, low = D.fetch_tile_filled(_F({"A": a, "T": np.full((256, 256), 5.0, np.float32)}),
                                   _chain(), 0, 0, 15)
    assert np.isnan(arr[100:120]).all() and not low.any()


def test_dem10b_only_where_no_dem5(fake_decode):
    t = np.full((256, 256), 30.0, np.float32)
    arr, low = D.fetch_tile_filled(_F({"A": np.full((256, 256), np.nan, np.float32), "T": t}),
                                   _chain(), 10, 20, 15)
    assert (arr == 30).all() and low.all()


def test_stops_when_no_hole(fake_decode):
    f = _F({"A": np.ones((256, 256), np.float32)})
    arr, low = D.fetch_tile_filled(f, _chain(), 0, 0, 15)
    assert len(f.urls) == 1 and not low.any()


def test_network_failure_is_not_a_hole(fake_decode):
    class Down(_F):
        def fetch(self, url):
            return None
    with pytest.raises(D.DemFetchError):
        D.fetch_tile_filled(Down({}), _chain(), 0, 0, 15)


def test_nothing_anywhere(fake_decode):
    assert D.fetch_tile_filled(_F({}), _chain(), 0, 0, 15) == (None, None)


# ── 水域マスク（2026-09-30）: 水域の外の大きな穴だけ DEM10B で埋める ──

def _water(rows):
    m = np.zeros((256, 256), bool)
    m[rows] = True
    return lambda x, y: m


def test_large_land_hole_is_filled_but_water_is_not(fake_decode):
    a = np.full((256, 256), 10.0, np.float32)
    a[:128] = np.nan                          # 上半分が欠損（うち 0〜19 行は水域）
    t = np.full((256, 256), 30.0, np.float32)
    arr, low = D.fetch_tile_filled(_F({"A": a, "T": t}), _chain(), 0, 0, 15,
                                   water=_water(slice(0, 20)), land_hole_min_frac=0.10)
    assert np.isnan(arr[:20]).all()           # ★水面は埋めない
    assert (arr[20:128] == 30).all() and low[20:128].all()
    assert (arr[128:] == 10).all() and not low[128:].any() and not low[:20].any()


def test_small_land_hole_is_left(fake_decode):
    """★川原の縁など、水域の外の欠損が閾値未満なら埋めない（取りにも行かない）。"""
    a = np.full((256, 256), 10.0, np.float32)
    a[:30] = np.nan                           # 30/256 = 11.7% の欠損、うち 20 行は水域 → 陸は 3.9%
    f = _F({"A": a, "T": np.full((256, 256), 30.0, np.float32)})
    arr, low = D.fetch_tile_filled(f, _chain(), 0, 0, 15,
                                   water=_water(slice(0, 20)), land_hole_min_frac=0.10)
    assert np.isnan(arr[:30]).all() and not low.any()
    assert not any(u.startswith("T") for u in f.urls)


def test_no_dem5_tile_keeps_water_empty(fake_decode):
    t = np.full((256, 256), 30.0, np.float32)
    arr, low = D.fetch_tile_filled(_F({"A": np.full((256, 256), np.nan, np.float32), "T": t}),
                                   _chain(), 0, 0, 15, water=_water(slice(200, 256)),
                                   land_hole_min_frac=0.10)
    assert (arr[:200] == 30).all() and low[:200].all()
    assert np.isnan(arr[200:]).all() and not low[200:].any()
