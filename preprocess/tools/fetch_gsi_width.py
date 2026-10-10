"""地域ぶんの国土地理院ベクトルタイル（RdCL）を取得する。★NK-18 の差し替えに要る。

★NK-18 の決定（2026-09-20）: **経路計算の道幅は地理院の幅員区分に統一する。**
  PLATEAU の実測と混ぜると、市境をまたいだ瞬間に `μ·narrow` の効き方が変わる。

P-4（藤沢・2026-09-20）で採用を決めた根拠:
  無作為な2本の大小一致 **93.7%**（OSM 道路種別は 85.9%）
  Spearman ρ 0.741 vs 0.694 ／ 区分内のばらつきも小さい

★2026-09-25 から既定は **手元の PMTiles から読む**（`gsi_vector.source: pmtiles`。通信しない）。
  XYZ 版は 2023年10月1日時点で更新停止（→ config.yaml と kansui.pmtiles）。
  `source: xyz` のときだけ従来どおり1枚ずつ取る（絶対ルール3: 逐次・sleep・同時接続1・
  キャッシュ・UA 明示。再実行はキャッシュから返る）。

★最適化ベクトルタイルは**試験公開**。「URL やデータ構成、属性の有無や名称等が
  変わる可能性があります」と国土地理院が明記している。
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, box

from kansui import boundary as B
from kansui.config import Config
from kansui.fetcher import TileFetcher
from kansui.gsi_vector import ROAD_LAYER, VectorTiles, decode_layer, tile_bbox_of
from kansui.region import Region

OUT = Path("data/raw/gsi")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("kansui.fetcher").setLevel(logging.WARNING)
    cfg = Config.load()
    region = Region.from_env(cfg)
    gdf = B.load(TileFetcher.from_config(cfg), cfg.require("boundary.url_template"),
                 region, cfg.resolve_path("boundary.cache_dir"))
    # ★グラフは地域+3km まで伸びているので、幅員もそこまで要る
    poly = (gdf.to_crs(3857).buffer(region.buffer_km * 1000.0).to_crs(4326).union_all()
            if region.buffer_km else gdf.union_all())

    z = int(cfg.require("gsi_vector.zoom"))
    from kansui.gsi_vector import tiles_for_bbox

    w, s, e, n = poly.bounds
    cand = tiles_for_bbox(w, s, e, n, z)
    targets = [(x, y) for x, y in cand if box(*tile_bbox_of(x, y, z)).intersects(poly)]
    print(f"地域 {region.name}（+{region.buffer_km:g}km）", flush=True)
    print(f"z{z} タイル: bbox {len(cand):,} → ★実際に重なる {len(targets):,} 枚\n", flush=True)

    source = cfg.require("gsi_vector.source")
    if source == "pmtiles":
        from kansui.pmtiles import PMTiles

        pm = cfg.resolve_path("gsi_vector.pmtiles_path")
        if not pm.exists():
            # ★黙って XYZ（2023年10月で止まったデータ）へ戻らない
            print(f"★{pm} が無い。取得すること:\n  curl -A '<UA>' -C - -o {pm}.part "
                  f"{cfg.require('gsi_vector.pmtiles_url')} && mv {pm}.part {pm}", flush=True)
            return 2
        vt = PMTiles(pm)
        if vt.max_zoom < z:
            raise SystemExit(f"PMTiles の最大ズーム {vt.max_zoom} < 必要な z{z}")
        print(f"読み出し元: {pm}（手元・通信しない）", flush=True)
    elif source == "xyz":
        from kansui.fetcher import recently_throttled

        cache = cfg.resolve_path("tiles.cache_dir")
        slow = float(cfg.require("tiles.sleep_after_throttle_sec"))
        sleep = float(cfg.require("tiles.sleep_sec"))
        if recently_throttled(cache, float(cfg.require("tiles.throttle_memory_sec"))):
            sleep = max(sleep, slow)   # ★最近 429/503 を受けていたら減速して始める
        vt = VectorTiles(cache_dir=cache,
                         user_agent=cfg.require("tiles.user_agent"),
                         sleep_sec=sleep,
                         sleep_after_throttle_sec=slow,
                         max_retry_after_sec=float(cfg.require("tiles.max_retry_after_sec")),
                         max_retries=int(cfg.require("tiles.max_retries")),
                         retry_backoff_sec=float(cfg.require("tiles.retry_backoff_sec")))
    else:
        raise SystemExit(f"gsi_vector.source が不正: {source!r}（pmtiles / xyz）")
    geoms, props = [], []
    t0 = time.time()
    for i, (x, y) in enumerate(targets, 1):
        data = vt.fetch(z, x, y)
        # 手元から読むときは速いので、進捗は間引いて出す
        if i % (20000 if source == "pmtiles" else 200) == 0 or i == len(targets):
            el = time.time() - t0
            print(f"  {i:>6}/{len(targets)}  {el / 60:6.1f}分経過 / "
                  f"残り約{(el / i) * (len(targets) - i) / 60:6.1f}分  {vt.stats}", flush=True)
        if data is None:
            continue
        for lines, p in decode_layer(data, z, x, y, ROAD_LAYER):
            for ln in lines:
                geoms.append(LineString(ln))
                props.append(p)

    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"rdcl_{region.name}.parquet"
    gpd.GeoDataFrame(props, geometry=geoms, crs=4326).to_parquet(out)
    print(f"\n★RdCL {len(geoms):,} 本 → {out}", flush=True)
    print(f"取得統計: {vt.stats}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
