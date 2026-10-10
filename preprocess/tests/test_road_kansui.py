"""NK-09: 確定の点を区間に結び付ける（2026-10-02）。"""
import numpy as np
import pandas as pd

from kansui import road_kansui as RK


def _edges():
    # 0: 下の道（東西） 1: その逆向き 2: 上を通る橋（南北） 3: 遠くの道
    lon0 = np.array([139.0000, 139.0010, 139.0005, 139.1000])
    lat0 = np.array([35.0000, 35.0000, 34.9995, 35.1000])
    lon1 = np.array([139.0010, 139.0000, 139.0005, 139.1010])
    lat1 = np.array([35.0000, 35.0000, 35.0005, 35.1000])
    return lon0, lat0, lon1, lat1


def test_snaps_to_road_below_not_bridge():
    lon0, lat0, lon1, lat1 = _edges()
    pts = pd.DataFrame({"lon": [139.0005], "lat": [35.0000]})
    label = ["下の道", "下の道", "橋", "遠く"]
    elig = np.array([True, True, False, True])          # ★橋は結び付けない
    m, ok = RK.known_mask(pts, lon0, lat0, lon1, lat1, label, elig, 30, 40)
    assert ok == [True] and m.tolist() == [True, True, False, False]


def test_too_far_is_not_snapped():
    lon0, lat0, lon1, lat1 = _edges()
    pts = pd.DataFrame({"lon": [139.05], "lat": [35.05]})
    m, ok = RK.known_mask(pts, lon0, lat0, lon1, lat1, ["a"] * 4, np.ones(4, bool), 30, 40)
    assert ok == [False] and not m.any()


def test_unnamed_marks_reverse_only():
    lon0, lat0, lon1, lat1 = _edges()
    pts = pd.DataFrame({"lon": [139.0005], "lat": [35.0000]})
    m, ok = RK.known_mask(pts, lon0, lat0, lon1, lat1, [None] * 4, np.array([True, True, False, True]), 30, 40)
    assert m.tolist() == [True, True, False, False]


def test_display_records_never_emit_nan():
    """★空欄が NaN のまま JSON に出ると、画面が黙って読めなくなる（2026-10-03）。"""
    import json
    pts = pd.DataFrame([{"id": "x", "tier": "unlocated", "lon": "", "lat": "", "radius_m": "", "route": float("nan"),
                         "place": "p", "manager": None, "method": "m", "src_title": "t", "src_publisher": "pb",
                         "src_date": "d", "src_url": "u", "check_page": "1", "check_hint": "h"}])
    out = RK.display_records(pts)
    json.dumps(out, allow_nan=False)
    assert out[0]["route"] == "" and out[0]["manager"] == ""
