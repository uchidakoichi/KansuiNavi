"""P-1: 地域ぶんの標高タイルを取得し、DEM5A の被覆率を測る。

★問い（設計 §5 P-1）: 1地域ぶんの標高を、国土地理院に無理をかけずに取れるか。
  測るもの: 実タイル枚数 / 所要時間 / 転送量 / **DEM5A の被覆率**（NK-07 の地域版）

★絶対ルール3 のまま: 逐次・sleep 1.0秒・同時接続1・キャッシュ・UA 明示。
  再実行はキャッシュから返るので、途中で止めても続きから進む（中断・再開可能）。
"""

from __future__ import annotations

import logging
import sys
import time

from kansui import boundary as B
from kansui import dem as D
from kansui.config import Config
from kansui.fetcher import TileFetcher
from kansui.region import Region


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = Config.load()
    region = Region.from_env(cfg)
    fetcher = TileFetcher.from_config(cfg)

    gdf = B.load(fetcher, cfg.require("boundary.url_template"), region,
                 cfg.resolve_path("boundary.cache_dir"))
    poly = gdf.union_all()
    if region.buffer_km:
        # ★度で膨らませない。メートルで膨らませてから戻す
        poly = (gdf.to_crs(3857).buffer(region.buffer_km * 1000.0)
                .to_crs(4326).union_all())

    chain = D.fallback_chain(cfg.require("dem.sources"), cfg.require("dem.primary"))
    print(f"地域: {region.name}（+{region.buffer_km:g}km）", flush=True)
    print(f"フォールバック順: {' → '.join(s.name for s in chain)}", flush=True)

    t0 = time.time()

    def progress(i, n, x, y, name):
        if i % 100 == 0 or i == n:
            el = time.time() - t0
            print(f"  {i:>5}/{n}  {el / 60:5.1f}分経過  "
                  f"残り約{(el / i) * (n - i) / 60:5.1f}分  {fetcher.stats.summary()}",
                  flush=True)

    st = D.fetch_region(fetcher, chain, poly, on_tile=progress)

    print(f"\n{'=' * 70}\n★P-1 結果\n{'=' * 70}", flush=True)
    print(st.report(chain), flush=True)
    print(f"\n所要 {(time.time() - t0) / 60:.1f} 分", flush=True)
    print(fetcher.stats.summary(), flush=True)

    # ★全国への外挿（実際には全国を取らない）
    area_km2 = B.geodesic_area_km2(poly)
    print(f"\n対象面積 {area_km2:,.0f} km² → "
          f"1 km² あたり {st.tiles / area_km2:.2f} 枚", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
