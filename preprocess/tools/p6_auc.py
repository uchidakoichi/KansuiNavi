"""P-6: 市ごとの AUC ── S_topo は内水の浸水想定区域を予測できるか。

★問い（設計 §5 P-6）: 藤沢以外でも当たるか。**地域差がどれだけあるかを知ること**が目的。
  ★**合格ラインは置かない。** 藤沢の 0.72 も「中程度」であって、他市が下回っても
    それ自体は中止理由にならない。

正解データ（2026-09-20 に実タイルを叩いて確認）:
  平塚市・横須賀市・綾瀬市 ── ハザードマップポータルの内水タイル
  藤沢市                  ── PLATEAU の ifld ベクタ（藤沢版が既に持っている。★回帰テスト）
  ★神奈川県 58市区町村のうち、正解があるのはこの4市だけ。

★**評価範囲は市域内の道路サンプル点**。藤沢版 V-4 は「排水区域内」に限っていたので
  **0.7197 とは直接比べられない。** ここでは4市を同じ基準で並べることを優先する。

★T-02: **無着色＝安全ではない。** 内水レイヤは 0〜0.1m を描画しないし、
  「指定が無い」と「浸水しない」は違う。陰性クラスにはその両方が混ざっている。
  → 陽性率を必ず併記する（AP の基準線になる）。

★T-12 は起きない。Δh は**サンプル点でのみ**正確な円で計算する（`kansui.terrain`）。
"""

from __future__ import annotations

import io
import logging
import sys
from collections import Counter
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from PIL import Image
from rasterio.warp import Resampling, calculate_default_transform, reproject

from kansui import boundary as B
from kansui import dem as D
from kansui.config import Config
from kansui.fetcher import NOT_FOUND, TileFetcher
from kansui.region import N03_CITY, N03_CODE, Region, hazard_url, region_epsg
from kansui.terrain import compute_delta_h, sample_edges
from kansui.tiles import TILE_SIZE, tile_range_for_bbox, tile_transform_3857

FUJISAWA_RAW = Path(__file__).resolve().parents[3] / "Kansui/preprocess/data/raw"  # 藤沢版のリポジトリを隣に置いた前提
ROADS = Path("data/raw/roads")
OUT = Path("data/raw/p6")

#: 対象。★藤沢だけ正解データの出所が違う。
#
#   ★★団体コードは**推測せずに N03 から引く**こと。
#     2026-09-20 に綾瀬市を `14212` と書いて実際に間違えた。**14212 は厚木市**で、
#     厚木市には内水の配信が無いため、正解ラスタが全ゼロのまま
#     「AUC が計算できない」という形でしか表に出なかった。
#     → 下の `_resolve` で**市名から引き、見つからなければ止める**。
TARGET_CITIES = {"平塚市": "tile", "横須賀市": "tile", "綾瀬市": "tile",
                 "藤沢市": "plateau"}


def _resolve(gdf, names: dict[str, str]) -> dict[str, tuple[str, str]]:
    """市名 → (団体コード, 正解の出所)。★見つからなければ例外で止める。"""
    d = gdf.drop_duplicates(N03_CODE)
    out = {}
    for name, kind in names.items():
        row = d[d[N03_CITY] == name]
        if row.empty:
            raise SystemExit(f"★{name} が N03 に無い。市名か対象地域を確認すること")
        out[str(row[N03_CODE].iloc[0])] = (name, kind)
    return out

#: 内水レイヤの凡例（藤沢版 config の `hazard.layers.naisui.legend_lut`）
#: ★T-06: 洪水/高潮とは**下限が違う**。共通化しない。
NAISUI_LUT = {
    "#DC7ADC": 8, "#F285C9": 7, "#FF9191": 6, "#FFB7B7": 5,
    "#FFD8C0": 4, "#F8E1A6": 3, "#F7F5A9": 2, "#FFFFB3": 1,
}
D_REF_M = 1.0


def _rgb(h: str) -> tuple[int, int, int]:
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


LUT = {_rgb(k): v for k, v in NAISUI_LUT.items()}


# -----------------------------------------------------------------------------
# 指標 ── ★外部ライブラリを足さずに済ませる
# -----------------------------------------------------------------------------
def auc(score: np.ndarray, label: np.ndarray) -> float:
    """Mann-Whitney U による AUC。同点は平均順位で扱う。"""
    order = np.argsort(score, kind="mergesort")
    ranks = np.empty(len(score), dtype=np.float64)
    ranks[order] = np.arange(1, len(score) + 1)
    s = score[order]
    i = 0
    while i < len(s):                       # 同点をまとめて平均順位に
        j = i
        while j + 1 < len(s) and s[j + 1] == s[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = (i + j + 2) / 2
        i = j + 1
    n_pos = int(label.sum())
    n_neg = len(label) - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    return float((ranks[label == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def average_precision(score: np.ndarray, label: np.ndarray) -> float:
    """PR 曲線下の面積（階段和）。★基準線は陽性率そのもの。"""
    order = np.argsort(-score, kind="mergesort")
    lab = label[order]
    tp = np.cumsum(lab)
    prec = tp / np.arange(1, len(lab) + 1)
    n_pos = int(label.sum())
    return float((prec * lab).sum() / n_pos) if n_pos else float("nan")


# -----------------------------------------------------------------------------
# ラスタ作り
# -----------------------------------------------------------------------------
def _mosaic_3857(fetch, z: int, bounds, cell, nodata, dtype):
    """z15 のタイル格子そのままで 3857 ラスタを組む（再サンプリングしない）。"""
    tr = tile_range_for_bbox(bounds, z)
    h, w = tr.height * TILE_SIZE, tr.width * TILE_SIZE
    arr = np.full((h, w), nodata, dtype=dtype)
    for x, y in tr:
        a = fetch(x, y)
        if a is None:
            continue
        r0, c0 = (y - tr.y_min) * TILE_SIZE, (x - tr.x_min) * TILE_SIZE
        arr[r0:r0 + TILE_SIZE, c0:c0 + TILE_SIZE] = a
    return arr, tile_transform_3857(tr.x_min, tr.y_min, z), tr.count


def _write(path: Path, arr, transform, crs, nodata, dtype):
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1],
                       count=1, dtype=dtype, crs=crs, transform=transform,
                       nodata=nodata, compress="deflate") as d:
        d.write(arr, 1)
    return path


def _reproject(src_path: Path, dst_path: Path, epsg: int, res_m: float,
               resampling, nodata, dtype):
    with rasterio.open(src_path) as src:
        transform, width, height = calculate_default_transform(
            src.crs, f"EPSG:{epsg}", src.width, src.height, *src.bounds, resolution=res_m
        )
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(dst_path, "w", driver="GTiff", crs=f"EPSG:{epsg}",
                           transform=transform, width=width, height=height, count=1,
                           dtype=dtype, nodata=nodata, compress="deflate") as dst:
            reproject(rasterio.band(src, 1), rasterio.band(dst, 1),
                      src_nodata=nodata, dst_nodata=nodata, resampling=resampling)
    return dst_path


# -----------------------------------------------------------------------------
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("kansui.fetcher").setLevel(logging.WARNING)
    cfg = Config.load()
    region = Region.from_config(cfg.require("region"))
    f = TileFetcher.from_config(cfg)
    gdf = B.load(f, cfg.require("boundary.url_template"), region,
                 cfg.resolve_path("boundary.cache_dir"))
    epsg = region_epsg(gdf)
    res_m = float(cfg.require("risk.delta_h.analysis_resolution_m"))
    radii = [float(r) for r in cfg.require("risk.delta_h.radii_m")]
    n_az = int(cfg.require("risk.delta_h.n_azimuths"))
    chain = D.fallback_chain(cfg.require("dem.sources"), cfg.require("dem.primary"))
    zdem = chain[0].zoom

    edges = gpd.read_parquet(ROADS / f"edges_{region.name}.parquet").to_crs(epsg)
    interval = 20.0   # 藤沢版と同じ
    OUT.mkdir(parents=True, exist_ok=True)

    print(f"地域 {region.name} / EPSG:{epsg} / 解析格子 {res_m}m / "
          f"Δh 半径 {radii} / 方位 {n_az}\n", flush=True)
    print(f"{'市':<10}{'点数':>9}{'陽性率':>9}{'AUC':>9}{'AP':>9}"
          f"{'標高のみAUC':>13}{'正解の出所':>12}", flush=True)
    print("-" * 74, flush=True)

    targets = _resolve(gdf, TARGET_CITIES)
    print("対象: " + " / ".join(f"{n}({c})" for c, (n, _) in targets.items()) + "\n",
          flush=True)

    results = []
    for code, (name, kind) in targets.items():
        row = gdf[gdf[N03_CODE].astype(str) == code]
        city = row.to_crs(epsg).union_all()
        halo = city.buffer(max(radii) + 50.0)      # ★Δh の半径ぶん外まで標高が要る

        # --- 標高ラスタ（キャッシュ済みタイルから組む） ------------------------
        dem_tif = OUT / f"dem_{code}.tif"
        if not dem_tif.exists():
            wgs = gpd.GeoSeries([halo], crs=epsg).to_crs(4326).iloc[0].bounds
            def fetch_dem(x, y, _chain=chain, _z=zdem):
                _, a = D.fetch_tile_with_fallback(f, _chain, x, y, _z)
                return a
            arr, tf, n = _mosaic_3857(fetch_dem, zdem, wgs, None, np.nan, "float32")
            _write(OUT / f"dem3857_{code}.tif", arr, tf, "EPSG:3857", np.nan, "float32")
            _reproject(OUT / f"dem3857_{code}.tif", dem_tif, epsg, res_m,
                       Resampling.bilinear, np.nan, "float32")

        # --- 正解ラスタ -------------------------------------------------------
        truth_tif = OUT / f"truth_{code}.tif"
        if not truth_tif.exists():
            if kind == "tile":
                url = hazard_url("naisui", code[:2])
                unknown: Counter = Counter()
                def fetch_haz(x, y, _url=url, _z=zdem, _unknown=unknown):
                    d = f.fetch(_url.format(z=_z, x=x, y=y))
                    if d is NOT_FOUND or d is None:
                        return None
                    rgba = np.asarray(Image.open(io.BytesIO(d)).convert("RGBA"))
                    out = np.zeros(rgba.shape[:2], dtype=np.uint8)
                    opaque = rgba[..., 3] > 0
                    for rgb, cls in LUT.items():
                        out[np.all(rgba[..., :3] == rgb, axis=-1) & opaque] = cls
                    # ★想定外の色は黙って捨てず数える（LUT の前提が崩れた合図）
                    miss = opaque & (out == 0)
                    if miss.any():
                        for c in map(tuple, rgba[..., :3][miss][:50]):
                            _unknown[c] += 1
                    return out
                wgs = gpd.GeoSeries([city], crs=epsg).to_crs(4326).iloc[0].bounds
                arr, tf, _ = _mosaic_3857(fetch_haz, zdem, wgs, 0, 0, "uint8")
                if unknown:
                    print(f"  ★{name}: 凡例に無い色 {len(unknown)} 種"
                          f"（上位 {unknown.most_common(3)}）", flush=True)
                _write(OUT / f"truth3857_{code}.tif", arr, tf, "EPSG:3857", 0, "uint8")
                _reproject(OUT / f"truth3857_{code}.tif", truth_tif, epsg, res_m,
                           Resampling.nearest, 0, "uint8")
            else:
                _reproject(FUJISAWA_RAW / "validation" / "naisui_plateau.tif",
                           truth_tif, epsg, res_m, Resampling.nearest, 0, "uint8")

        # --- サンプル点 -------------------------------------------------------
        # ★bbox で粗く絞ってから正確に判定する（1.45M 本を総当たりしない）
        cand = edges.iloc[list(edges.sindex.query(city, predicate="intersects"))]
        sub = cand[cand.geometry.intersects(city)]
        pts, _ = sample_edges(sub, interval)
        print(f"  {name}: エッジ {len(sub):,} / サンプル点 {len(pts):,}", flush=True)
        inside = gpd.GeoSeries(gpd.points_from_xy(pts[:, 0], pts[:, 1]), crs=epsg).within(city)
        pts = pts[inside.to_numpy()]

        dh = compute_delta_h(dem_tif, pts, radii, epsg=epsg, n_azimuths=n_az)
        s_topo = np.sqrt(np.prod(
            [np.exp(-dh[f"delta_h_{int(r)}"] / D_REF_M) for r in radii], axis=0))

        with rasterio.open(truth_tif) as src:
            inv = ~src.transform
            t = src.read(1)
            c, r = inv * (pts[:, 0], pts[:, 1])
            r, c = np.rint(r).astype(int), np.rint(c).astype(int)
            ok = (r >= 0) & (r < t.shape[0]) & (c >= 0) & (c < t.shape[1])
            label = np.zeros(len(pts), dtype=np.int8)
            label[ok] = (t[r[ok], c[ok]] > 0).astype(np.int8)

        valid = np.isfinite(s_topo) & np.isfinite(dh["elevation"])
        s, lab, elev = s_topo[valid], label[valid], dh["elevation"][valid]
        a, ap = auc(s, lab), average_precision(s, lab)
        a_elev = auc(-elev, lab)          # ★低いほど危険なので符号を反転
        print(f"{name:<10}{len(s):>9,}{lab.mean():>9.4f}{a:>9.4f}{ap:>9.4f}"
              f"{a_elev:>13.4f}{('タイル' if kind == 'tile' else 'PLATEAU'):>12}",
              flush=True)
        results.append({"code": code, "name": name, "n": len(s),
                        "pos": float(lab.mean()), "auc": a, "ap": ap, "auc_elev": a_elev})

    print("\n★合格ラインは置かない。地域差がどれだけあるかを見るための測定である。", flush=True)
    print("★評価範囲は市域内の道路サンプル点。藤沢版 V-4（排水区域内 87,801点・AUC 0.7197）"
          "\n  とは範囲が違うので直接は比べられない。", flush=True)
    print("★T-02: 無着色＝安全ではない。陰性には「指定が無い」と「浸水しない」が混ざる。",
          flush=True)
    import json
    (OUT / "p6_auc.json").write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(f"\n{f.stats.summary()}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
