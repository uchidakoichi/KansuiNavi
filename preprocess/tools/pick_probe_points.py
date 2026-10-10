"""配布済みの区画ファイルから、経路の検査に使う地点を**データから**選ぶ。

★地図を見て打った緯度経度は使わない。データを作り直すたびに意味が変わるため。
  ここで選んだ点を `web/tools/route_probe.mjs` に環境変数で渡す。

選ぶもの:
  near / mid / far  … 巨大強連結成分の中の2点（直線 2-5km / 5-15km / 15-40km）
  isolated          … ★巨大強連結成分の**外**のノード（＝到達不能な断片）
  isolated_from     … その断片の近くにある、つながっているノード
                      （★遠い点と組にすると 40km の上限で先に断られ、狙いが測れない）
  sea               … ★どの道路からも遠い点（海の上の想定）
  sea_from          … 海の点の近くにある、つながっているノード
  bands             … ★距離帯ごとの標本（`--bands N` で1帯あたり N 組）。
                      性能要件（NK-06）を**帯ごとに**決めるために要る。
                      ★実機で測れるのは数本なので、**端末に依らない「確定ノード数」**を
                      ここで多数集め、実機で測った「1ノードあたりの時間」と掛ける。

使い方:
  uv run python tools/pick_probe_points.py > /tmp/probe.json
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import numpy as np

from kansui.export_mesh import read_mesh

DATA = Path("../web/public/data")
FLAG_MAIN = 1
#: ★再現できるように種を固定する（決め打ちの座標を書かないため）。`--seed N` で変えられる
SEED = 20260921


def _load(idx: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    lons, lats, flags = [], [], []
    for code in sorted(idx["meshes"]):
        buf = gzip.decompress((DATA / "mesh" / f"{code}.bin.gz").read_bytes())

        # ★形式 4: 配置と道路名は区画ファイルの先頭にある（→ export_mesh.read_mesh）
        _arrs, _ = read_mesh(buf)

        def arr(name: str, _a=_arrs):
            return _a[name]

        ox, oy = idx["origin"]
        s = idx["coordScale"]
        lons.append(arr("nodeLon") / s + ox)
        lats.append(arr("nodeLat") / s + oy)
        # ★「つながっている」は最大の成分（番号 1）。形式 5
        flags.append(arr("nodeComp") == 1)
    return np.concatenate(lons), np.concatenate(lats), np.concatenate(flags)


def _km(alon, alat, blon, blat) -> np.ndarray:
    k = np.cos(np.radians((alat + blat) / 2))
    return np.hypot((blon - alon) * k * 111.32, (blat - alat) * 110.54)


def _pair(rng, lon, lat, main, lo_km, hi_km):
    """巨大成分の中から、直線距離が範囲に入る2点を引く。

    ★1点目を引き、2点目は**その点から範囲に入るノードの中から**引く（2026-10-09）。
      2点を独立に引くと、全国のデータでは 0.5〜5km の組がまず出ない（2000回で尽きる）。
    """
    ids = np.where(main)[0]
    for _ in range(200):
        a = int(rng.choice(ids))
        dlat = hi_km / 110.54
        dlon = hi_km / (111.32 * np.cos(np.radians(lat[a])))
        box = ids[(np.abs(lat[ids] - lat[a]) <= dlat) & (np.abs(lon[ids] - lon[a]) <= dlon)]
        d = _km(lon[a], lat[a], lon[box], lat[box])
        cand = box[(d >= lo_km) & (d <= hi_km)]
        if len(cand):
            b = int(rng.choice(cand))
            return [[float(lon[a]), float(lat[a])], [float(lon[b]), float(lat[b])]]
    raise SystemExit(f"★{lo_km}〜{hi_km}km の組が引けなかった")


#: ★`router.worker.ts` の `MAX_SNAP_M`（1000m）と同じ値。**実装と揃える**
MAX_SNAP_KM = 1.0
#: ★距離帯。上限 40km は `routing.max_route_distance_km`（宣言している上限）に合わせる
BANDS = [(0.5, 5.0), (5.0, 15.0), (15.0, 25.0), (25.0, 40.0)]


def main() -> int:
    idx = json.loads((DATA / "index.json").read_text(encoding="utf-8"))
    lon, lat, flags = _load(idx)
    main = flags.astype(bool)
    seed = SEED
    for i, a in enumerate(sys.argv):
        if a == "--seed" and i + 1 < len(sys.argv):
            seed = int(sys.argv[i + 1])
    rng = np.random.default_rng(seed)

    # ★★孤立した断片。**つながっている道から `MAX_SNAP_M` より遠いもの**を選ぶ。
    #   2026-09-21、ここを 300m にしていて検査が機能していなかった:
    #   937m 先のつながった道へ**正しく寄せてしまい**、0分の経路が返って
    #   「断る」ところを試せていなかった。**閾値は実装と揃える。**
    off = np.where(~main)[0]
    on = np.where(main)[0]
    isolated = None
    for i in rng.permutation(off)[:2000]:
        d = _km(lon[on], lat[on], lon[i], lat[i])
        if d.min() > MAX_SNAP_KM:
            isolated = [float(lon[i]), float(lat[i])]
            break
    if isolated is None:
        raise SystemExit(
            f"★つながった道から {MAX_SNAP_KM:g}km 以上離れた孤立ノードが見つからない。"
            "★見つからないこと自体が良い報せなので、黙って近いもので代用しないこと"
        )

    # ★海の上。全ノードの外接矩形の南（相模湾）へ 0.15度 出す
    sea = [float(lon.max()) - 0.05, float(lat.min()) - 0.15]

    def nearest_main(pt):
        """★その点の近くで「つながっている」ノード。**40km の上限に引っかからせない**ため。"""
        d = _km(lon[on], lat[on], pt[0], pt[1])
        j = on[int(np.argmin(d))]
        return [float(lon[j]), float(lat[j])]

    out = {
        "near": _pair(rng, lon, lat, main, 2, 5),
        "mid": _pair(rng, lon, lat, main, 5, 15),
        "far": _pair(rng, lon, lat, main, 15, 40),
        "isolated": isolated,
        "isolated_from": nearest_main(isolated),
        "sea": sea,
        "sea_from": nearest_main(sea),
    }
    # ★距離帯ごとの標本（`--bands N`）
    n_band = 0
    for i, a in enumerate(sys.argv):
        if a == "--bands" and i + 1 < len(sys.argv):
            n_band = int(sys.argv[i + 1])
    if n_band:
        out["bands"] = [
            {
                "lo": lo,
                "hi": hi,
                "pairs": [_pair(rng, lon, lat, main, lo, hi) for _ in range(n_band)],
            }
            for lo, hi in BANDS
        ]

    print(json.dumps(out, ensure_ascii=False))
    print(
        f"# ノード {len(lon):,} / 巨大成分 {main.sum():,}（{main.mean():.2%}）"
        f" / 外 {len(off):,}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
