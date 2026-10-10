"""F-01 住所・交差点名の検索データを全国ぶん作る（NK-12 決定 2026-09-28: OSM を同梱）。

    uv run python tools/build_places.py

★手元の PBF と N03 のキャッシュだけを読む。**通信しない。**
★藤沢版（`../Kansui/preprocess/src/kansui/addresses.py`）と同じ作り:
  OSM の place（町丁目・大字・地区）と、名前の付いた信号交差点。
  **番地では探せない。網羅は保証しない。**
★行政区域（N03）で都道府県名・市区町村名を付ける。「本町」のような同名が全国に多いため。
★配布の対象範囲（index.json の cover3）の外は落とす。クライアントは範囲の外を断るので、
  選べない候補を出さない。
★道路ノードへは寄せない（藤沢版は寄せていた）。全国版のクライアントは、タップと同じく
  座標を渡し、ワーカーが連結成分を見て寄せる（寄せ距離の上限もワーカーが持つ）。

出力（`places.out`）: 列ごとの配列を持つ JSON。座標は 1e-5 度の整数（約1m）。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

from kansui import boundary as B
from kansui.config import Config
from kansui.mesh import mesh_code
from kansui.region import N03_CITY, N03_CODE, N03_PREF

INDEX = Path("../web/public/data/index.json")
#: ★PBF から取り出した直後の表（13分かかる）。PBF の顔ぶれと大きさが同じなら使い回す
RAW = Path("data/raw/places_raw.parquet")
#: N03（2026年版）で政令指定都市の区が入る列。★N03_004 は市、N03_005 は区（実データで確認）
N03_WARD = "N03_005"


def _names(df: pd.DataFrame) -> pd.Series:
    """名前。pyrosm は name を列に出すことも tags にだけ残すこともある（四国で tags のみだった）。"""
    def one(a, tags):
        if isinstance(a, str) and a:
            return a
        if isinstance(tags, str) and tags:
            try:
                tags = json.loads(tags)
            except ValueError:
                return None
        if isinstance(tags, dict):
            v = tags.get("name") or tags.get("name:ja")
            return v if isinstance(v, str) and v else None
        return None

    a = df["name"] if "name" in df.columns else pd.Series([None] * len(df), index=df.index)
    t = df["tags"] if "tags" in df.columns else pd.Series([None] * len(df), index=df.index)
    return pd.Series([one(x, y) for x, y in zip(a, t, strict=True)], index=df.index)


def _points(gdf: gpd.GeoDataFrame) -> gpd.GeoSeries:
    """面は代表点（面の内側に必ず入る点）。重心は面の外に出ることがある。"""
    return gdf.geometry.apply(lambda g: g if g.geom_type == "Point" else g.representative_point())


def extract(pbf: Path, kinds: dict[str, str], signals: bool) -> pd.DataFrame:
    from pyrosm import OSM

    osm = OSM(str(pbf))
    parts = []
    p = osm.get_pois(custom_filter={"place": True})
    if p is not None and len(p):
        p = p[p["place"].isin(kinds)]
        p = p.assign(name=_names(p))
        p = p[p["name"].notna()]
        pt = _points(p)
        parts.append(pd.DataFrame({"name": p["name"].to_numpy(),
                                   "kind": p["place"].map(kinds).to_numpy(),
                                   "lon": pt.x.to_numpy(), "lat": pt.y.to_numpy()}))
    if signals:
        s = osm.get_data_by_custom_criteria(
            custom_filter={"highway": ["traffic_signals"]}, filter_type="keep",
            keep_nodes=True, keep_ways=False, keep_relations=False)
        if s is not None and len(s):
            s = s.assign(name=_names(s))
            s = s[s["name"].notna()]
            parts.append(pd.DataFrame({"name": s["name"].to_numpy(), "kind": "交差点",
                                       "lon": s.geometry.x.to_numpy(), "lat": s.geometry.y.to_numpy()}))
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(
        columns=["name", "kind", "lon", "lat"])


def municipalities(cfg, df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """点ごとの「都道府県 市区町村」と団体コード。★N03 のキャッシュだけを読む（無ければ止まる）。"""
    cache = cfg.resolve_path("boundary.cache_dir")
    zips = sorted(Path(cache).glob("N03-*_GML.zip"))
    if len(zips) != 47:
        raise SystemExit(f"★N03 のキャッシュが {len(zips)} 件（47 件要る）: {cache}")
    n03 = gpd.GeoDataFrame(pd.concat([B.to_geodataframe(B.read_zip(z)) for z in zips],
                                     ignore_index=True), crs=6668).to_crs(4326)
    # ★郡名は付けない（「高座郡寒川町」より「寒川町」の方が探す人の言葉に近い）。
    #   政令指定都市は区を付ける（「横浜市港北区」）。区だけでは同名が多い
    ward = n03[N03_WARD].fillna("") if N03_WARD in n03.columns else ""
    n03["label"] = n03[N03_PREF].fillna("") + " " + n03[N03_CITY].fillna("") + ward
    pts = gpd.GeoDataFrame(geometry=[Point(x, y) for x, y in zip(df["lon"], df["lat"], strict=True)],
                           crs=4326, index=df.index)
    j = gpd.sjoin(pts, n03[["label", N03_CODE, "geometry"]], how="left", predicate="within")
    j = j[~j.index.duplicated(keep="first")]
    return j["label"].reindex(df.index), j[N03_CODE].reindex(df.index)


def in_coverage(df: pd.DataFrame, idx: dict) -> np.ndarray:
    """★クライアントの対象範囲（web/src/coverage.ts の buildCoverage）と**同じ判定**。

    3次メッシュの印 → 正方形の窓で閉じ処理（膨張 k → 収縮 k。枠の外は 0）→ 4近傍で穴埋め。
    ★生の印だけで判定すると、クライアントが受け付ける範囲の点を 1,965 件落としていた（7回目のレビュー）。
    """
    from scipy import ndimage

    from kansui.mesh import mesh_bbox

    lat3, lon3 = 30, 45
    k = max(0, int(idx.get("coverClosing") or 0))
    cells = []
    for code2, bits in idx["cover3"].items():
        w, s, _, _ = mesh_bbox(code2)
        r0, c0 = round(s * 3600 / lat3), round(w * 3600 / lon3)
        cells += [(r0 + i // 10, c0 + i % 10) for i in range(100) if bits[i] == "1"]
    rc = np.array(cells)
    pad = k + 1
    rmin, cmin = rc.min(0) - pad
    rmax, cmax = rc.max(0) + pad
    grid = np.zeros((rmax - rmin + 1, cmax - cmin + 1), dtype=bool)
    grid[rc[:, 0] - rmin, rc[:, 1] - cmin] = True
    if k > 0:
        st = np.ones((2 * k + 1, 2 * k + 1), dtype=bool)
        grid = ndimage.binary_erosion(ndimage.binary_dilation(grid, st), st, border_value=0)
    grid = ndimage.binary_fill_holes(grid)   # ★4近傍（既定の十字）＝クライアントのたどり方と同じ
    lat_s = np.round(df["lat"].to_numpy() * 3600 * 1e6) / 1e6
    lon_s = np.round(df["lon"].to_numpy() * 3600 * 1e6) / 1e6
    r = np.floor(lat_s / lat3).astype(np.int64) - rmin
    c = np.floor(lon_s / lon3).astype(np.int64) - cmin
    ok = (r >= 0) & (r < grid.shape[0]) & (c >= 0) & (c < grid.shape[1])
    out = np.zeros(len(df), dtype=bool)
    out[ok] = grid[r[ok], c[ok]]
    return out


def merge(df: pd.DataFrame, radius: dict[str, float]) -> pd.DataFrame:
    """同じ市区町村・同じ名前・同じ種別で近いものを1つに畳む（座標は平均）。"""
    rows = []
    for (name, kind, muni), g in df.groupby(["name", "kind", "muni"], sort=False):
        r = float(radius.get(kind, radius["_other"]))
        lon, lat = g["lon"].to_numpy(), g["lat"].to_numpy()
        cy = np.cos(np.radians(lat.mean()))
        left = list(range(len(g)))
        while left:
            i = left[0]
            d = np.hypot((lon[left] - lon[i]) * cy, lat[left] - lat[i]) * 111_320.0
            near = [left[k] for k in np.flatnonzero(d <= r)]
            rows.append((name, kind, muni, float(lon[near].mean()), float(lat[near].mean()), len(near)))
            gone = set(near)
            left = [k for k in left if k not in gone]
    return pd.DataFrame(rows, columns=["name", "kind", "muni", "lon", "lat", "n"])


def main() -> int:
    cfg = Config.load()
    pc = cfg.require("places")
    kinds = dict(pc["kinds"])
    idx = json.loads(INDEX.read_text(encoding="utf-8"))
    pbfs = sorted({cfg.resolve_path(f"osm.extracts.{k}.path")
                   for k in cfg.require("osm.extracts")})
    t0 = time.time()
    stamp = json.dumps([[p.name, p.stat().st_size] for p in pbfs] + [sorted(kinds.items()),
                                                                     bool(pc["signals"])])
    if RAW.exists() and RAW.with_suffix(".stamp").exists() \
            and RAW.with_suffix(".stamp").read_text(encoding="utf-8") == stamp:
        df = pd.read_parquet(RAW)
        print(f"取り出し済みの表を使う（{RAW}・{len(df):,} 件）", flush=True)
    else:
        parts = []
        for pbf in pbfs:
            d = extract(pbf, kinds, bool(pc["signals"]))
            print(f"{pbf.name}: {len(d):,} 件（{time.time() - t0:.0f}秒）", flush=True)
            parts.append(d)
        df = pd.concat(parts, ignore_index=True)
        df.to_parquet(RAW)
        RAW.with_suffix(".stamp").write_text(stamp, encoding="utf-8")
    n0 = len(df)
    keep = in_coverage(df, idx)
    df = df[keep].reset_index(drop=True)
    print(f"対象範囲の外を落とした: {n0:,} → {len(df):,}", flush=True)
    df["muni"], df["code"] = municipalities(cfg, df)
    miss = int(df["muni"].isna().sum())
    df = df[df["muni"].notna()].reset_index(drop=True)
    print(f"市区町村に入らない点を落とした（海上・境界の外）: {miss:,}", flush=True)
    code_of = df.groupby("muni")["code"].min().to_dict()
    df = merge(df, dict(pc["merge_m"]))
    # ★交差点は同じ市区町村・同じ名前を1つに畳む（藤沢版と同じ。信号の名前の付いた通りなどは
    #   離れた信号が同じ名前を持ち、見分けの付かない候補が並ぶ。7回目のレビュー）。いちばん大きい塊を採る
    sig = df["kind"] == "交差点"
    if sig.any():
        n = df[sig].groupby(["name", "muni"])["n"].transform("max")
        keep_sig = df[sig][df[sig]["n"] == n].drop_duplicates(["name", "muni"])
        df = pd.concat([df[~sig], keep_sig], ignore_index=True)
    # ★並び順 = 検索結果の順。住所（藤沢版と同じく町丁目・大字・地区）を先に、交差点を後に。
    #   同じ種別の中は団体コード順（北から）。文字順だと「三重県」が先頭に来る
    rank = {k: i for i, k in enumerate(list(kinds.values()) + ["交差点"])}
    df["_r"] = df["kind"].map(rank)
    df["_c"] = df["muni"].map(code_of)
    df = df.sort_values(["_r", "_c", "name"], kind="stable").reset_index(drop=True)

    kinds_out = sorted(df["kind"].unique(), key=rank.get)
    munis = sorted(df["muni"].unique(), key=code_of.get)
    km = {k: i for i, k in enumerate(kinds_out)}
    mm = {m: i for i, m in enumerate(munis)}
    import hashlib

    out = {
        "version": 1,
        # ★どの対象範囲で絞ったか（書き出しが範囲の食い違いを警告する。→ export_national）
        "cover3Hash": hashlib.sha1(json.dumps(idx["cover3"], sort_keys=True).encode()).hexdigest(),
        "source": "OpenStreetMap contributors (ODbL)",
        "pbf": [p.name for p in pbfs],
        "maxResults": int(pc["max_results"]),
        "kinds": kinds_out,
        "munis": munis,
        "name": df["name"].tolist(),
        "k": [km[k] for k in df["kind"]],
        "m": [mm[m] for m in df["muni"]],
        "lon": np.rint(df["lon"].to_numpy() * 1e5).astype(np.int64).tolist(),
        "lat": np.rint(df["lat"].to_numpy() * 1e5).astype(np.int64).tolist(),
    }
    dst = cfg.resolve_path("places.out")
    tmp = dst.with_suffix(".json.part")
    tmp.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(dst)
    vc = df["kind"].value_counts().to_dict()
    print(f"\n★{len(df):,} 件（{vc}）／ 市区町村 {len(munis):,} → {dst}"
          f"（{dst.stat().st_size / 1e6:.1f}MB・{time.time() - t0:.0f}秒）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
