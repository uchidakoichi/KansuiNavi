"""地域のノードに標高を付ける（F-03 ルート標高プロファイル用）。

    KANSUI_REGION=saitama uv run python tools/node_elevation.py

★**キャッシュ済みの標高タイルだけを読む。通信しない**（→ kansui.node_elev）。
  作成の連鎖と並行で流してよい。キャッシュに無いタイルのノードは NaN のまま。
★出力は data/raw/elev/{地域}.parquet（ノードの鍵 → 標高［m］）。書き出しがこれを引く。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from kansui import dem as D
from kansui import node_elev as NE
from kansui.config import Config
from kansui.region import Region

FINAL = Path("data/raw/final")
OUT = Path("data/raw/elev")


def main() -> int:
    cfg = Config.load()
    region = Region.from_env(cfg)
    df = pd.read_parquet(FINAL / f"{region.name}.parquet",
                         columns=["u", "v", "lon0", "lat0", "lon1", "lat1"])
    key = np.concatenate([df["u"].to_numpy(), df["v"].to_numpy()])
    lon = np.concatenate([df["lon0"].to_numpy(), df["lon1"].to_numpy()])
    lat = np.concatenate([df["lat0"].to_numpy(), df["lat1"].to_numpy()])
    key, first = np.unique(key, return_index=True)
    lon, lat = lon[first], lat[first]
    # ★作成（build_region）と同じ系列。NK-07 決定（2026-09-28）から DEM10B も使う
    chain = D.fallback_chain(cfg.require("dem.sources"), cfg.require("dem.primary"))
    if bool(cfg.require("dem.exclude_dem10b")):
        chain = [s for s in chain if s.name != "dem10b"]
    t0 = time.time()
    elev, which = NE.sample(lon, lat, cfg.resolve_path("tiles.cache_dir"), chain, with_source=True)
    # ★主系列よりズームの小さい種別（DEM10B）から取った点。プロファイルで破線にする
    low_idx = [i for i, s in enumerate(chain) if s.zoom < chain[0].zoom]
    low = np.isin(which, low_idx)
    OUT.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"key": key, "elev": elev, "low": low}).to_parquet(OUT / f"{region.name}.parquet")
    miss = int(np.isnan(elev).sum())
    print(f"  DEM10B から取った点: {int(low.sum()):,}", flush=True)
    print(f"{region.name}: ノード {len(key):,} / 標高不明 {miss:,} ({miss / len(key):.3%})"
          f" / 範囲 {np.nanmin(elev):.1f}〜{np.nanmax(elev):.1f}m / {time.time() - t0:.0f}秒",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
