"""地域の配布データを作る（★区画ごとに処理する本番の形）。

★★**県全域を一度に処理しない。** 神奈川県を 4m 格子で舐めると
  437M 画素＝1.75GB になり、メモリに載らない。
  → **区画（2次メッシュ）ごとに、余裕（Δh の半径ぶん）を足して処理する。**
  これは P-2 / P-7 で決めた分割の単位そのものなので、
  **中断・再開の粒度も揃う**（区画ごとに parquet を残す）。

手順（区画ごと）:
  1. 区画 ＋ 余裕（Δh の最大半径＋α）の DEM ラスタを組む（★キャッシュ済みタイルから）
  2. その区画に属するエッジを 20m 間隔でサンプリング
  3. サンプル点で Δh（★正確な円。T-12 は起きない）
  4. S_topo = √(exp(−Δh₁₀₀/d)·exp(−Δh₂₅₀/d))
  5. ★エッジへの集約は**最大値**（T-04）。1点でも危険ならそのリンクは危険

最後に:
  - アンダーパスの幾何推定（T-01）で `state` を上書き（★採点せず判定不能）
  - 区画ごとの配布データを書き出す（`kansui.export_mesh`）
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.warp import Resampling, calculate_default_transform, reproject
from shapely.geometry import box

from kansui import boundary as B
from kansui import connectivity as C
from kansui import dem as D
from kansui import underpass as U
from kansui import width as W
from kansui.config import Config
from kansui.fetcher import TileFetcher
from kansui.mesh import mesh_bbox
from kansui.region import Region, region_epsg
from kansui.terrain import compute_delta_h, sample_edges
from kansui.tiles import TILE_SIZE, tile_bounds_lonlat, tile_range_for_bbox, tile_transform_3857

ROADS = Path("data/raw/roads")
GSI = Path("data/raw/gsi")
WORK_ROOT = Path("data/raw/build")
#: ★地域ごとの最終表。配布データへは `tools/export_national.py` がまとめる
FINAL = Path("data/raw/final")
#: ★S_topo の尺度 d とサンプル間隔。**config から読む**（main の冒頭で上書き）。
#:   2026-09-24 まで直書きで、config の risk.delta_h.d_ref_m を変えても効かなかった。
D_REF_M = 1.0
INTERVAL_M = 20.0

#: 藤沢版 config の `routing.road_class_narrowness`。
#: ★NK-18 で「道幅は地理院の幅員区分に統一する」と決めたので、**これは退避**である。
#:   区分が付かなかったエッジ（照合できなかった・区分の綴りが変わった 等）だけに使う。
#:   ★0 で埋めない。0 は「最も広い道」を意味し、不明を優先してしまう（→ `width.py`）。
NARROW_BY_HIGHWAY = {
    "living_street": 1.0, "residential": 0.8, "unclassified": 0.6,
    "tertiary": 0.3, "tertiary_link": 0.3, "secondary": 0.15, "secondary_link": 0.15,
    "trunk": 0.05, "trunk_link": 0.05, "primary": 0.0, "primary_link": 0.0,
    "motorway": 0.0, "motorway_link": 0.0,
}


def _cache_fingerprint(edges, chain, radii, res_m: float, n_az: int, margin_m: float,
                       water_key: str) -> str:
    """区画ごとの S_topo キャッシュの同一性を表す指紋。

    ★★**S_topo を決めるものを全部入れる。**
      2026-09-22、最初はエッジ表だけを見ていた。それだと
      **DEM の系列や Δh の半径を変えてもキャッシュが再利用され、
      古い値のまま「作り直した」ことになる。**
      ★自分で入れた守りに穴を空けていた。**鍵は「答えを決めるもの全部」にする。**

    入れるもの:
      エッジ表   … 行数と両端ノードの並び（位置索引の意味が変わったか）
      DEM の系列 … 名前・ズーム・URL（種別を1つ外しただけでも変わる）
      Δh の設定  … 半径・解析解像度・方位数
      取る範囲   … 道路からの余裕（小さすぎると円の縁が欠損になり答えが変わる）
    """
    import hashlib

    h = hashlib.sha256()
    h.update(str(len(edges)).encode())
    for col in ("u", "v", "mesh_from", "mesh_to"):
        if col in edges.columns:
            h.update(np.ascontiguousarray(edges[col].to_numpy().astype("U32")).tobytes())
    for src in chain:
        h.update(f"{src.name}|{src.zoom}|{src.url}".encode())
    h.update(f"radii={sorted(radii)}|res={res_m}|az={n_az}|dref={D_REF_M}|iv={INTERVAL_M}".encode())
    h.update(f"margin={margin_m}".encode())
    # ★DEM の組み方と印の出し方（v3: 5A→5B→5C は画素単位で埋め、DEM10B は DEM5 の無いタイルだけ。
    #   印は円の中の DEM10B。2026-09-29）
    h.update(b"dem=pixel-fill-v4-10b-land-holes|low=disk")
    # ★v4（2026-09-30）: 水域の外の大きな穴も DEM10B で埋める（水域マスクと閾値も鍵に入れる）
    h.update(water_key.encode())
    return h.hexdigest()


#: ★切り出して拡大する処理は kansui.dem に移した（画素単位の穴埋めでも使うため）
upsample_part = D.upsample_part


def _low_at(dem_path: Path, pts: np.ndarray, radius_m: float) -> np.ndarray:
    """サンプル点の **Δh を見る円（半径 radius_m）の中**に DEM10B で埋めた画素があるか（dem_for のバンド2）。

    ★★2026-09-29、最初は「サンプル点の画素そのものが DEM10B か」で印を付けていた。
      それでは、点は DEM5 でも**円の中に DEM10B の画素が入って Δh が変わった区間**に印が付かず、
      作り直しの前後で値が変わったのに「DEM5 だけで推定」と表示していた（34地域・1〜396本）。
      → 円の中に1画素でもあれば印を付ける（NK-07「区別して明示する」の趣旨）。範囲外は False。
    """
    from scipy.ndimage import distance_transform_edt

    with rasterio.open(dem_path) as src:
        band = src.read(2) > 0.5
        res = float(src.res[0])
        rows, cols = rasterio.transform.rowcol(src.transform, pts[:, 0], pts[:, 1])
    rows, cols = np.asarray(rows), np.asarray(cols)
    ok = (rows >= 0) & (rows < band.shape[0]) & (cols >= 0) & (cols < band.shape[1])
    out = np.zeros(len(pts), dtype=bool)
    if not band.any():
        return out
    # 各画素から最寄りの DEM10B 画素までの距離［画素］。★1画素ぶんの余裕（円の縁の丸め）
    dist = distance_transform_edt(~band)
    out[ok] = dist[rows[ok], cols[ok]] * res <= radius_m + res
    return out


def dem_for(fetcher, chain, poly_wgs, epsg: int, res_m: float, dst: Path,
            need_wgs=None, water_mask=None, land_hole_min_frac: float | None = None) -> Path:
    """その範囲の標高ラスタを組んで、地域の平面直角座標系へ再投影する。

    ★`need_wgs`（経緯度の面）を渡すと、**それに重なるタイルだけ取る**（config の
      dem.fetch_margin_m）。★ラスタの枠と格子は `poly_wgs` で決めたまま変えない。
      取らないタイルは欠損のまま —— 格子が同じなら、Δh の円に入る画素の値は変わらない。
    """
    if dst.exists():
        return dst
    z = chain[0].zoom
    zoom_of = {src.name: src.zoom for src in chain}
    tr = tile_range_for_bbox(poly_wgs.bounds, z)
    arr = np.full((tr.height * TILE_SIZE, tr.width * TILE_SIZE), np.nan, dtype=np.float32)
    #: ★画素ごとの「低い精度の種別（主系列よりズームの小さいもの＝DEM10B）から来たか」。1 = そう
    low = np.zeros(arr.shape, dtype=np.uint8)
    n_low = 0
    if need_wgs is not None:
        import shapely

        shapely.prepare(need_wgs)
    for x, y in tr:
        if need_wgs is not None and not need_wgs.intersects(box(*tile_bounds_lonlat(x, y, z))):
            continue
        a, lowt = D.fetch_tile_filled(fetcher, chain, x, y, z, water=water_mask,
                                      land_hole_min_frac=land_hole_min_frac)
        if a is None:
            continue
        # ★★画素単位で DEM5A → 5B → 5C → 10B の順に穴を埋める（→ kansui.dem.fetch_tile_filled）。
        #   ズームの小さい種別（DEM10B・z14）は、主系列のタイルに当たる部分を切り出して拡大してから使う
        #   （そのまま貼ると標高が縦横とも2倍に圧縮される。2026-09-22 の監査）。
        #   ★NK-07 決定（2026-09-28・利用者判断「ないよりはまし」）で DEM10B を使う。
        r0, c0 = (y - tr.y_min) * TILE_SIZE, (x - tr.x_min) * TILE_SIZE
        arr[r0:r0 + TILE_SIZE, c0:c0 + TILE_SIZE] = a
        if lowt.any():
            low[r0:r0 + TILE_SIZE, c0:c0 + TILE_SIZE] = lowt
            n_low += 1
    if n_low:
        print(f"  DEM10B で埋めたタイル {n_low} 枚", flush=True)

    tmp = dst.with_suffix(".3857.tif")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    meta = dict(driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=2,
                dtype="float32", crs="EPSG:3857", nodata=np.nan, compress="deflate")
    with rasterio.open(tmp, "w", transform=tile_transform_3857(tr.x_min, tr.y_min, z),
                       **meta) as f:
        f.write(arr, 1)
        f.write(low.astype(np.float32), 2)
    with rasterio.open(tmp) as src:
        t, w, h = calculate_default_transform(src.crs, f"EPSG:{epsg}", src.width, src.height,
                                              *src.bounds, resolution=res_m)
        part = dst.with_suffix(".part.tif")
        # ★バンド1 = 標高（双線形）、バンド2 = DEM10B 由来の印（最近傍）。
        #   compute_delta_h はバンド1だけを読む
        with rasterio.open(part, "w", driver="GTiff", crs=f"EPSG:{epsg}", transform=t,
                           width=w, height=h, count=2, dtype="float32", nodata=np.nan,
                           compress="deflate") as d:
            reproject(rasterio.band(src, 1), rasterio.band(d, 1), src_nodata=np.nan,
                      dst_nodata=np.nan, resampling=Resampling.bilinear)
            reproject(rasterio.band(src, 2), rasterio.band(d, 2), src_nodata=np.nan,
                      dst_nodata=np.nan, resampling=Resampling.nearest)
    tmp.unlink()
    # ★書き終えてから置き換える。途中で止まった（空き不足・強制終了）ラスタを
    #   次の実行で「できている」と読まない
    part.replace(dst)
    return dst


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("kansui.fetcher").setLevel(logging.WARNING)
    logging.getLogger("kansui.terrain").setLevel(logging.WARNING)
    cfg = Config.load()
    region = Region.from_env(cfg)
    fetcher = TileFetcher.from_config(cfg)
    gdf = B.load(fetcher, cfg.require("boundary.url_template"), region,
                 cfg.resolve_path("boundary.cache_dir"))
    epsg = region_epsg(gdf)
    global D_REF_M, INTERVAL_M
    D_REF_M = float(cfg.require("risk.delta_h.d_ref_m"))
    INTERVAL_M = float(cfg.require("risk.delta_h.sample_interval_m"))
    if not bool(cfg.require("routing.exclude_flat_grade_separation")):
        raise SystemExit("★routing.exclude_flat_grade_separation=false は未実装"
                         "（検出した立体交差はすべて判定不能にしている）")
    # ★面積の検算（読み込みと座標系の回帰検知）。公式値のある団体が地域に含まれていれば当てる
    from kansui.region import N03_CODE

    tol = float(cfg.require("boundary.area_check_tolerance"))
    for code, km2 in (cfg.get("boundary.official_area_km2", {}) or {}).items():
        if str(code) in set(gdf[N03_CODE].astype(str)):
            got = B.check_area(gdf, str(code), float(km2), tol)
            print(f"面積の検算: {code} {got:.2f} km²（公式 {km2}・許容 ±{tol:.0%}）", flush=True)
    res_m = float(cfg.require("risk.delta_h.analysis_resolution_m"))
    radii = [float(r) for r in cfg.require("risk.delta_h.radii_m")]
    n_az = int(cfg.require("risk.delta_h.n_azimuths"))
    margin_m = float(cfg.require("dem.fetch_margin_m"))
    chain = D.fallback_chain(cfg.require("dem.sources"), cfg.require("dem.primary"))
    # ★★T-07: DEM10B は等高線由来の 10m メッシュで、標高の分解能が粗い（SD 5.0m）。
    #   凹地検出が成立しないので**リスク算出の対象外として明示する**（推定値で埋めない）。
    #   ★`config.yaml` に `exclude_dem10b` があるのに**誰も読んでいなかった**
    #     （2026-09-22 の監査で判明。T-07 が全国版では未実装だった）。
    # ★★NK-07 決定（2026-09-28）で DEM10B を使うようにした（config の dem.exclude_dem10b: false）。
    #   使うときは dem_for が z15 の枠へ拡大して貼り、区間に「DEM10B で推定」の印を付ける
    if bool(cfg.require("dem.exclude_dem10b")):
        before = [s.name for s in chain]
        chain = [s for s in chain if s.name != "dem10b"]
        if len(chain) != len(before):
            print("★T-07: DEM10B を外した（分解能不足。該当区画は判定不能になる）", flush=True)
    if not chain:
        raise SystemExit("★DEM の系列が空になった。config の dem.sources を確認すること")
    # ★★キャッシュは**地域ごとの置き場**に分ける。印（.cache.sha256）は1地域ぶんの主張なので、
    #   同じ置き場を複数の地域で使うと、地域を替えるたびに他地域のキャッシュを捨ててしまう。
    WORK = WORK_ROOT / region.name
    WORK.mkdir(parents=True, exist_ok=True)

    edges = gpd.read_parquet(ROADS / f"edges_{region.name}.parquet")
    print(f"地域 {region.name} / EPSG:{epsg} / エッジ {len(edges):,}", flush=True)

    # ★★**S_topo のキャッシュを「答えを決めるもの全部」に紐づける。**
    #   区画ごとの `WORK/{code}.parquet` は S_topo を**エッジの位置索引**で持つ。
    #   エッジ表・DEM の系列・Δh の設定のどれが変わっても、
    #   **古いキャッシュを使うと間違った値が間違った道に付く。**
    #   ★例外も警告も出ない。2026-09-21 に実際に踏みかけ、手で消して回避していた。
    # ★★水域の外の大きな穴を DEM10B で埋める（2026-09-30。→ kansui.dem.fetch_tile_filled）。
    #   水域は手元の PMTiles（国土地理院 最適化ベクトルタイル）の WA。通信しない
    water = None
    land_hole_min_frac = float(cfg.require("dem.land_hole_min_frac"))
    water_key = "water=off"
    if bool(cfg.require("dem.water_mask")) and any(s.zoom < chain[0].zoom for s in chain):
        from kansui.water import WaterMask

        pm = cfg.resolve_path("gsi_vector.pmtiles_path")
        if not pm.exists():
            raise SystemExit(f"★水域マスクの PMTiles が無い: {pm}")
        water = WaterMask(pm, z=chain[0].zoom).mask
        water_key = f"water=WA@{pm.name}:{pm.stat().st_size}|min={land_hole_min_frac}"
    fp = _cache_fingerprint(edges, chain, radii, res_m, n_az, margin_m, water_key)
    stamp = WORK / ".cache.sha256"
    have = stamp.read_text(encoding="utf-8").strip() if stamp.exists() else None
    # ★★**印が無いキャッシュは「正しい」ではなく「確かめられない」。捨てる。**
    #   2026-09-22、最初は `stamp.exists() and ...` と書いていたため、
    #   **印を導入した最初の1回だけ無条件に再利用**してしまった。
    #   実際その回で DEM10B を外したのに、古い S_topo がそのまま使われかけた
    #   （区画が 0.0分で終わるのを見て気づいた）。
    #   ★「分からないときは使わない」に倒す。作り直す時間より、
    #     間違った値が間違った道に付く方がずっと高くつく。
    if have != fp:
        # ★中間ラスタ dem_*.tif も捨てる。これも DEM の系列・解像度で決まるので、
        #   残すと**旧設定のラスタから新しい S_topo を作る**（2026-09-23 のレビューで指摘）
        stale = sorted(WORK.glob("*.parquet")) + sorted(WORK.glob("dem_*.tif"))
        for f in stale:
            f.unlink()
        stamp.unlink(missing_ok=True)
        if stale:
            why = "前提が変わった" if have else "前提を確かめられない（印が無い）"
            print(f"★{why}ので Δh のキャッシュを捨てた（{len(stale)} 区画）", flush=True)
    # ★★**印はここで書かない。全区画を作り終えてから書く。**
    #   2026-09-22、先に書いてしまい、その実行を途中で止めたせいで
    #   **「古いキャッシュなのに印だけ新しい」**状態を自分で作った。
    #   次の実行は印が一致するので再利用してしまい、**同じ誤りを繰り返した。**
    #   → 印は「全部そろっている」という主張なので、**そろってから書く。**
    #     途中で止まれば印が無いままになり、次は捨てる側に倒れる（安全側）。

    # ★★**Δh のキャッシュをエッジ表の版に紐づける。**
    #
    #   区画ごとの `WORK/{code}.parquet` は **エッジの位置索引**（`iloc`）で
    #   S_topo を持っている。エッジ表を作り直すと索引の意味が変わるので、
    #   **古いキャッシュを使うと別の道路の値が入る。**
    #   ★例外も警告も出ない。**間違ったリスクが間違った道に付くだけ。**
    #
    #   2026-09-21、道路を +3km で切り直したときに実際に踏みかけた
    #   （そのときは手で `rm data/raw/build/*.parquet` して回避した）。
    #   **手順を覚えていることを前提にしない。**

    # ★区画ごとの担当エッジ（またぐものは両方に入れる）
    by_mesh: dict[str, list[int]] = {}
    for i, (a, b) in enumerate(zip(edges["mesh_from"], edges["mesh_to"], strict=True)):
        by_mesh.setdefault(a, []).append(i)
        if a != b:
            by_mesh.setdefault(b, []).append(i)
    codes = sorted(by_mesh)
    print(f"区画 {len(codes)} 枚\n", flush=True)

    # --- 区画ごとに Δh → S_topo（★エッジには最大値で集約。T-04） ------------------
    s_topo = np.full(len(edges), np.nan, dtype=np.float32)
    #: ★DEM10B（精度の低い標高）を使って推定した区間（NK-07）
    dem_low = np.zeros(len(edges), dtype=bool)
    e6677 = edges.to_crs(epsg)
    t0 = time.time()
    for k, code in enumerate(codes, 1):
        part = WORK / f"{code}.parquet"
        if not part.exists():
            rows = np.array(by_mesh[code], dtype=np.int64)
            sub = e6677.iloc[rows]
            halo = box(*mesh_bbox(code))
            halo_m = (gpd.GeoSeries([halo], crs=4326).to_crs(epsg)
                      .buffer(max(radii) + 50.0).to_crs(4326).iloc[0])
            # ★取るのは、この区画の道路から「Δh の最大半径＋余裕」に重なるタイルだけ。
            #   ★距離は地域の平面直角座標系で測る（Web メルカトルでは緯度35度で約2割短い。T-05）
            need = (gpd.GeoSeries([sub.geometry.buffer(max(radii) + margin_m).union_all()],
                                  crs=epsg).to_crs(4326).iloc[0])
            dem = dem_for(fetcher, chain, halo_m, epsg, res_m, WORK / f"dem_{code}.tif",
                          need_wgs=need, water_mask=water, land_hole_min_frac=land_hole_min_frac)
            pts, owner = sample_edges(sub, INTERVAL_M)
            dh = compute_delta_h(dem, pts, radii, epsg=epsg, n_azimuths=n_az)
            st = np.sqrt(np.prod([np.exp(-dh[f"delta_h_{int(r)}"] / D_REF_M) for r in radii],
                                 axis=0))
            lowp = _low_at(dem, pts, max(radii))
            # ★リンクへの集約は最大値（T-04）。1点でも危険ならそのリンクは危険。
            #   ★DEM10B の印も「1点でも」（推定の中身に精度の低い標高が入っている）
            agg = (pd.DataFrame({"edge": rows[owner], "s": st, "low": lowp & np.isfinite(st)})
                   .groupby("edge").agg(s_topo=("s", "max"), dem_low=("low", "any")))
            agg.to_parquet(part)
            (WORK / f"dem_{code}.tif").unlink(missing_ok=True)   # 中間ラスタは残さない
        tab = pd.read_parquet(part)
        agg = tab["s_topo"]
        idx = agg.index.to_numpy()
        s_topo[idx] = np.fmax(np.nan_to_num(s_topo[idx], nan=-1.0), agg.to_numpy())
        dem_low[idx] |= tab["dem_low"].to_numpy(dtype=bool)
        el = time.time() - t0
        print(f"  {k:>3}/{len(codes)}  {code}  エッジ {len(by_mesh[code]):>6,}  "
              f"{el / 60:5.1f}分経過 / 残り約{(el / k) * (len(codes) - k) / 60:5.1f}分",
              flush=True)
    s_topo[s_topo < 0] = np.nan
    dem_low &= np.isfinite(s_topo)
    print(f"DEM10B で推定した区間: {int(dem_low.sum()):,} 本", flush=True)
    # ★全区画そろった。ここで初めて印を書く（→ 上の注記）
    stamp.write_text(fp, encoding="utf-8")
    print(f"\nS_topo: 算出 {np.isfinite(s_topo).sum():,} / {len(edges):,} "
          f"({np.isfinite(s_topo).mean():.1%})", flush=True)

    # --- アンダーパス（T-01）。★採点せず判定不能にハード上書き --------------------
    bridges = gpd.read_parquet(ROADS / f"bridges_{region.name}.parquet").to_crs(epsg)
    seps = U.find_grade_separations(e6677, bridges, epsg)
    seps = U.merge_with_tags(seps, e6677)
    seps["upper"] = [U.classify_upper(bridges, i) for i in seps["bridge_idx"]]
    keep = seps[seps["upper"] != "exclude"]
    state = pd.Series("topo", index=edges.index)
    # ★整数に直す。0件のとき空の列が整数型にならず、位置の代入で落ちる（2026-09-27 大東諸島）
    state.iloc[keep["edge_idx"].unique().astype(np.int64)] = "underpass"
    n_un = int((state == "underpass").sum())
    print(f"判定不能（アンダーパス）: {n_un:,} 本（{n_un / len(edges):.2%}）", flush=True)
    # ★判定不能は S_topo を持たせない。採点しないという意味を値でも表す
    s_topo[(state == "underpass").to_numpy()] = np.nan
    dem_low[(state == "underpass").to_numpy()] = False


    # --- 書き出し ---------------------------------------------------------------
    g4 = edges.to_crs(4326)
    coords = [np.asarray(g.coords) for g in g4.geometry]
    lon = np.array([[c[0][0], c[-1][0]] for c in coords])
    lat = np.array([[c[0][1], c[-1][1]] for c in coords])
    key = np.c_[edges["u"].to_numpy(), edges["v"].to_numpy()].astype(np.float64)
    fallback = edges["highway"].map(lambda h: NARROW_BY_HIGHWAY.get(h, 0.6)).to_numpy()

    # --- 道幅は国土地理院の幅員区分から（NK-18） ---------------------------------
    rdcl = GSI / f"rdcl_{region.name}.parquet"
    if not rdcl.exists():
        raise SystemExit(
            f"★{rdcl} が無い。`uv run python tools/fetch_gsi_width.py` を先に流すこと。\n"
            "  道路種別の近似で代用しない（NK-18 で地理院の区分に統一と決めたため）"
        )
    gsi = gpd.read_parquet(rdcl).to_crs(epsg)
    print(f"\n地理院 RdCL {len(gsi):,} 本 → 幅員区分を載せ替える", flush=True)
    rnk, wstat = W.assign(e6677, gsi, epsg, progress=lambda m: print(f"  {m}", flush=True))
    print(f"  照合できた {wstat['matched']:,} / {wstat['edges']:,} "
          f"({wstat['matched_share']:.2%})  "
          f"スナップ距離 中央値 {wstat['snap_median_m']:.2f}m / p90 {wstat['snap_p90_m']:.2f}m",
          flush=True)
    for k in W.RNKWIDTH_ORDER:
        c = wstat["by_class"].get(k, 0)
        print(f"    {k:<16}{c:>9,}  {c / max(1, wstat['matched']):>7.2%}"
              f"  → 狭さ {W.narrowness_table()[k]:.3f}", flush=True)
    narrow = W.to_narrowness(rnk, fallback)
    print(f"  ★退避（道路種別の近似）に落ちたエッジ: "
          f"{wstat['edges'] - wstat['matched']:,}", flush=True)

    # ★★`oneway=-1` は「幾何の向きと**逆**の一方通行」。
    #   両端を入れ替えてから配る。入れ替えないと、クライアントは
    #   **通れない向きに通れると思い込む**（神奈川県では 12本）。
    #   ★区画コードも一緒に入れ替える（両端の区画と両端のノードの対応を崩さない）。
    rev = C.reversed_mask(edges["oneway"])
    mesh_from = edges["mesh_from"].to_numpy().astype(object).copy()
    mesh_to = edges["mesh_to"].to_numpy().astype(object).copy()
    if rev.any():
        for arr in (lon, lat, key):
            arr[rev] = arr[rev][:, ::-1]
        mesh_from[rev], mesh_to[rev] = mesh_to[rev], mesh_from[rev].copy()
        print(f"★oneway=-1 の {int(rev.sum())} 本は両端を入れ替えた", flush=True)

    # ★★「この地域のエッジか」。書き出しで地域を重ねたとき、同じエッジ（+3km の重なり）は
    #   **始点が自分の区域の内側にある地域**のものを採る。値はほぼ同じはずだが、
    #   バッファの縁では橋や幅員の照合相手が欠けうるので、内側の方が確か。
    import shapely

    own = gdf.to_crs(4326).union_all()
    inside = shapely.contains_xy(own, lon[:, 0], lat[:, 0])
    print(f"始点が区域の内側: {int(inside.sum()):,} / {len(inside):,}", flush=True)

    FINAL.mkdir(parents=True, exist_ok=True)
    out = pd.DataFrame({
        "u": key[:, 0], "v": key[:, 1], "way": edges["id"].to_numpy(),
        "lon0": lon[:, 0], "lat0": lat[:, 0], "lon1": lon[:, 1], "lat1": lat[:, 1],
        "mesh_from": mesh_from.astype(str), "mesh_to": mesh_to.astype(str),
        "travel_time_s": edges["travel_time_s"].to_numpy(),
        "oneway": edges["oneway"].astype(str).to_numpy(),
        "name": edges["name"].to_numpy() if "name" in edges.columns else None,
        "ref": edges["ref"].to_numpy() if "ref" in edges.columns else None,
        "s_topo": s_topo, "dem_low": dem_low, "state": state.to_numpy(), "narrow": narrow,
        "inside": inside,
    })
    dst = FINAL / f"{region.name}.parquet"
    out.to_parquet(dst)
    print(f"\n★最終表 {len(out):,} 本 → {dst}", flush=True)
    print(f"  ★幅員照合 {wstat['matched_share']:.2%} / 判定不能 {n_un:,} 本", flush=True)
    print("  配布データへは `uv run python tools/export_national.py` でまとめる", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
