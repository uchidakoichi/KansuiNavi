"""区画ファイル（形式 4）の往復。★クライアント（meshgraph.ts）と同じ読み方で戻せること。"""

from __future__ import annotations

import gzip
import json

import numpy as np
import pandas as pd

from kansui import export_mesh as EM
from kansui.mesh import mesh_code


def _tiny(tmp_path):
    # 2つの2次区画をまたぐ3本（533946 の中の2本と、533945 へまたぐ1本）
    lon = np.array([[139.80, 139.81], [139.81, 139.82], [139.76, 139.74]])
    lat = np.array([[35.70, 35.70], [35.70, 35.71], [35.70, 35.70]])
    key = np.array([[1, 2], [2, 3], [4, 5]], dtype=np.float64)
    mf = np.array([mesh_code(x, y, 2) for x, y in zip(lon[:, 0], lat[:, 0], strict=True)])
    mt = np.array([mesh_code(x, y, 2) for x, y in zip(lon[:, 1], lat[:, 1], strict=True)])
    edges = pd.DataFrame({"travel_time_s": [10.0, 12.0, 30.0], "oneway": ["no", "yes", "no"],
                          "name": ["甲州街道", None, "青梅街道"], "ref": [None, "20", None]})
    risk = pd.DataFrame({"s_topo": [0.5, np.nan, 0.1], "state": ["topo", "underpass", "topo"]})
    flags = np.ones((3, 2), dtype=np.uint8)
    res = EM.export(edges, risk, mf, mt, key, lon, lat, flags, np.array([0.2, 0.5, 0.0]),
                    out_dir=tmp_path, region_name="t")
    return res, mf, mt


def test_roundtrip_keeps_names_per_mesh(tmp_path):
    res, mf, mt = _tiny(tmp_path)
    idx = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert idx["format"] == EM.FORMAT_VERSION == 8
    # ★index.json に道路名の表と配置を持たせない（全国で重くなるため）
    assert "names" not in idx
    assert all("layout" not in m for m in idx["meshes"].values())
    seen = set()
    for code, info in idx["meshes"].items():
        buf = gzip.decompress((tmp_path / "mesh" / f"{code}.bin.gz").read_bytes())
        assert len(buf) == info["bytes"]
        arrs, names = EM.read_mesh(buf)
        assert len(arrs["edgeName"]) == info["edges"]
        for i in arrs["edgeName"]:
            if i:
                seen.add(names[i - 1])
        # ★区画の表は、その区画に出てくる名前だけ
        assert len(names) == len(set(names))
    assert seen == {"甲州街道", "20", "青梅街道"}
    assert res["names"] == 3


def test_gzip_is_deterministic(tmp_path):
    """★同じ中身なら同じ .gz（作り直しの差分を追えるように）。"""
    _tiny(tmp_path / "a")
    _tiny(tmp_path / "b")
    for p in (tmp_path / "a" / "mesh").glob("*.gz"):
        assert p.read_bytes() == (tmp_path / "b" / "mesh" / p.name).read_bytes()


def test_wrong_magic_is_refused():
    import pytest

    with pytest.raises(EM.ExportError):
        EM.read_mesh(b"XXXX" + b"\0" * 16)


def test_zero_length_edge_gets_the_minimum_time(tmp_path):
    """★別のノードが同じ座標に重なった長さ 0 の区間（千葉で実在）で書き出しを止めない。"""
    import pandas as pd

    lon = np.array([[139.80, 139.80], [139.80, 139.81]])   # 1本目は長さ 0
    lat = np.array([[35.70, 35.70], [35.70, 35.70]])
    key = np.array([[1, 2], [2, 3]], dtype=np.float64)
    mf = np.array([mesh_code(x, y, 2) for x, y in zip(lon[:, 0], lat[:, 0], strict=True)])
    mt = np.array([mesh_code(x, y, 2) for x, y in zip(lon[:, 1], lat[:, 1], strict=True)])
    edges = pd.DataFrame({"travel_time_s": [0.0, 12.0], "oneway": ["no", "no"],
                          "name": [None, None]})
    risk = pd.DataFrame({"s_topo": [0.1, 0.1], "state": ["topo", "topo"]})
    EM.export(edges, risk, mf, mt, key, lon, lat, np.ones((2, 2), np.uint8),
              np.array([0.0, 0.0]), out_dir=tmp_path, region_name="t")
    idx = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    for code in idx["meshes"]:
        arrs, _ = EM.read_mesh(gzip.decompress((tmp_path / "mesh" / f"{code}.bin.gz").read_bytes()))
        assert (arrs["edgeTime"] >= 1).all()
