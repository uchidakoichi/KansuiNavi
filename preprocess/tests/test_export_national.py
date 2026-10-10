"""地域をまとめるときのノード座標の揃え方（tools/export_national.py）。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import export_national as X  # noqa: E402


def _case(inside=(True, True)):
    # ノード 7 を、地域 a（行0の v 側）と地域 b（行0の u 側）が別の座標で持つ
    node = np.array([7, 7])
    x = np.array([139.001, 139.00101])
    y = np.array([35.0, 35.0])
    return (node, x, y, np.array(inside), np.array([0, 1]), np.array([0, 0]),
            np.array([1, 0]))


def test_prefers_the_earlier_region():
    """★u 側を全部並べてから v 側、にすると優先度の低い地域が勝つ（実際に踏んだ）。"""
    fn, fx, fy = X.resolve_conflicts(*_case(), ["A", "B"])
    assert fn.tolist() == [7] and fx.tolist() == [139.001]


def test_prefers_the_region_whose_inside_it_is():
    fn, fx, _ = X.resolve_conflicts(*_case(inside=(False, True)), ["A", "B"])
    assert fx.tolist() == [139.00101]


def test_same_pbf_disagreement_is_refused():
    """★同じ PBF から作った地域どうしで食い違うのは、向きの取り違えなどの誤り。"""
    with pytest.raises(SystemExit):
        X.resolve_conflicts(*_case(), ["same", "same"])


def test_nothing_to_do_when_coordinates_agree():
    node, x, y, *rest = _case()
    x[1] = x[0]
    fn, _, _ = X.resolve_conflicts(node, x, y, *rest, ["A", "B"])
    assert len(fn) == 0


def _final(path, u, v, lon0, lon1, inside):
    import pandas as pd

    n = len(u)
    pd.DataFrame({
        "u": np.array(u, float), "v": np.array(v, float), "way": np.arange(n) + 100,
        "lon0": np.array(lon0, float), "lat0": np.full(n, 35.5),
        "lon1": np.array(lon1, float), "lat1": np.full(n, 35.5),
        "travel_time_s": np.full(n, 10.0, np.float32), "oneway": ["no"] * n,
        "name": [None] * n, "ref": [None] * n, "s_topo": np.full(n, 0.1, np.float32),
        "state": ["topo"] * n, "narrow": np.full(n, 0.5, np.float32),
        "inside": np.array(inside, bool),
    }).to_parquet(path)


def test_mixed_pbf_regions_go_through_to_mesh_codes(tmp_path, monkeypatch):
    """★PBF の日付が違う2地域で、共有ノードの座標がずれていても最後まで通ること。

    2026-09-23: pandas 3 の to_numpy が書き込めないビューを返し、補正の書き込みで落ちていた。
    同じ PBF の回帰（東京・神奈川＋埼玉）では補正が空なので、この経路を一度も通っていなかった。
    """
    from shapely.geometry import Polygon

    # a: ノード 1→2、b: ノード 2→3。ノード 2 の座標が a と b で 1m ほど違う
    _final(tmp_path / "a.parquet", [1], [2], [139.70], [139.71], [True])
    _final(tmp_path / "b.parquet", [2], [3], [139.71001], [139.72], [True])
    monkeypatch.setattr(X, "FINAL", tmp_path)
    monkeypatch.setattr(X, "region_polygon", lambda cfg, n: Polygon())
    monkeypatch.setattr(X, "road_fixes", lambda cfg, n, df: (np.zeros(len(df), bool),
                                                            np.full(len(df), np.nan, np.float32),
                                                            np.zeros(len(df), bool)))
    regs, info = X.select_rows(["a", "b"], None)
    fix = X.harmonize(regs, info["nodes"], ["A", "B"])
    assert len(fix[0]) == 1
    vmax, cover3 = X.globals_pass(regs, fix)
    assert vmax > 0 and cover3
    # ★揃えた座標（a 側の 139.71）から区画コードが付く
    assert regs[1].mf[0] == regs[0].mt[0]


def test_road_fixes_follow_the_osm_access_hierarchy_and_fix_speeds(tmp_path, monkeypatch):
    """★motorcar → motor_vehicle → vehicle → access の順。maxspeed の誤記は既定速度で直す。"""
    import json

    import pandas as pd

    from kansui.config import Config

    cfg = Config.load()
    n = 7
    pd.DataFrame({
        "u": list(range(1, n + 1)), "v": list(range(2, n + 2)),
        "highway": ["track", "residential", "residential", "busway", "residential",
                    "unclassified", "primary"],
        "access": ["no", "no", None, None, "private", None, None],
        "motor_vehicle": [None, None, "agricultural", None, None, None, None],
        "motorcar": [None, "yes", None, None, None, None, None],
        # 6本目: vehicle=forestry だけで閉じている林道（桜沢林道の形）
        "tags": [None] * 5 + [json.dumps({"vehicle": "forestry"}), None],
        "maxspeed": [None] * 6 + ["4"],
        "speed_kmh": [60.0] * 6 + [4.0],
        "speed_source": ["default"] * 6 + ["maxspeed"],
        "length": [100.0] * n,
    }).to_parquet(tmp_path / "edges_r.parquet")
    monkeypatch.setattr(X, "ROADS", tmp_path)
    final = pd.DataFrame({"u": [1.0, 3.0, 3.0, 4.0, 5.0, 6.0, 7.0],
                          "v": [2.0, 2.0, 4.0, 5.0, 6.0, 7.0, 8.0]})
    closed, tt, bridge = X.road_fixes(cfg, "r", final)
    assert not bridge.any()
    assert closed.tolist() == [True, False, True, True, True, True, False]
    # ★青梅街道（primary, maxspeed=4）の形: 既定速度で計算し直す
    d = cfg.require("osm.default_speed_kmh")
    v = float(d.get("primary", d["_other"]))
    assert np.isnan(tt[:6]).all()
    assert tt[6] == np.float32(100.0 / (np.float32(v) / 3.6))
