"""★区画をクライアントと同じ手順で統合し、元のグラフと**連結性が一致する**か確かめる。

★★これは外してはいけない検査である。
  境界ノードの同一視を間違えると、**区画の縁でグラフが分断される。**
  例外は出ない。経路が切れるだけで、しかも「その経路だけ出ない」ので
  気づきにくい。

  2026-09-21 に実際に踏んだ: またぐエッジの**片側の端点しか記録していなかった**ため、
  相手の区画が自分のノードの鍵を書かず、同じノードが二重に作られた。
    最大連結成分  統合後 35.76%  vs  元のグラフ 99.20%

★★**旗（`nodeFlags`）も同じ手順で検算する**（2026-09-21 追加）。
  配った旗が間違っていると、クライアントは「つながっている」と信じて
  到達不能なノードへ起終点を寄せ、**地域をほぼ全部読んでから諦める。**
  実測（修正前・神奈川・直線8km）: 区画 50/56 枚・16.5MB・確定ノード 1,293,073。
  → ここで**統合後のグラフから強連結成分を数え直し、1ビットでも違えば落とす。**
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from kansui.export_mesh import read_mesh

DATA = Path("../web/public/data")
#: 元のグラフ。★地域の edges parquet があればそれを、無ければ藤沢版の gpkg を見る
FINAL = Path("data/raw/final")
#: ★`kansui.connectivity` / `router.worker.ts` と同じ値
FLAG_MAIN = 1
FLAG_MAIN_NO_UNDERPASS = 2
#: ★`export_mesh.STATE_CODE["underpass"]`
STATE_UNDERPASS = 1
#: 元のグラフとの最大成分の割合の差。★これを超えたら分断を疑う
TOLERANCE = 0.005


def merge(data: Path):
    """★クライアント（`meshgraph.ts`）と同じ手順で統合する。実装を追うこと。"""
    idx = json.loads((data / "index.json").read_text(encoding="utf-8"))
    key2g: dict[float, int] = {}
    n_nodes = 0
    eu_all, ev_all, ow_all, st_all = [], [], [], []
    flags: dict[int, int] = {}
    comps: dict[int, tuple[int, int]] = {}
    for code in sorted(idx["meshes"]):
        info = idx["meshes"][code]
        buf = gzip.decompress((data / "mesh" / f"{code}.bin.gz").read_bytes())
        if len(buf) != info["bytes"]:
            raise SystemExit(f"★{code} の大きさが index と違う: {len(buf)} != {info['bytes']}")

        # ★形式 4: 配置と道路名は区画ファイルの先頭にある（→ export_mesh.read_mesh）
        _arrs, _ = read_mesh(buf)

        def arr(name: str, _a=_arrs):
            return _a[name]

        n = len(arr("nodeLon"))
        m = np.full(n, -1, dtype=np.int64)
        # ★またぐエッジの**両端**を見る。片方だけだと二重に作られる
        pairs = [(arr("bKey"), arr("bLocal")), (arr("fKey"), arr("fLocal"))]
        for keys, locals_ in pairs:
            for k, loc in zip(keys, locals_, strict=True):
                if k in key2g:
                    m[loc] = key2g[k]
        for i in range(n):
            if m[i] < 0:
                m[i] = n_nodes
                n_nodes += 1
        for keys, locals_ in pairs:
            for k, loc in zip(keys, locals_, strict=True):
                key2g.setdefault(float(k), int(m[loc]))
        eu_all.append(m[arr("edgeU")])
        ev_all.append(m[arr("edgeV")])
        ow_all.append(arr("edgeOneway"))
        st_all.append(arr("edgeState"))
        # ★同じノードは同じ旗のはず。**違ったらその場で落とす**（黙って上書きしない）
        nf = arr("nodeFlags")
        nc, ncx = arr("nodeComp"), arr("nodeCompX")
        for i in range(n):
            gi = int(m[i])
            if gi in flags and flags[gi] != int(nf[i]):
                raise SystemExit(
                    f"★{code} のノード {i}（統合後 {gi}）の旗が食い違う: "
                    f"{flags[gi]} != {int(nf[i])}"
                )
            flags[gi] = int(nf[i])
            c = (int(nc[i]), int(ncx[i]))
            if gi in comps and comps[gi] != c:
                raise SystemExit(f"★{code} のノード {i}（統合後 {gi}）の成分番号が食い違う: "
                                 f"{comps[gi]} != {c}")
            comps[gi] = c
    shipped = np.zeros(n_nodes, dtype=np.uint8)
    comp = np.zeros((n_nodes, 2), dtype=np.int64)
    for gi, f in flags.items():
        shipped[gi] = f
        comp[gi] = comps[gi]
    return (n_nodes, np.concatenate(eu_all), np.concatenate(ev_all),
            np.concatenate(ow_all), np.concatenate(st_all), shipped, comp,
            int(idx["connectivity"]["min_component_nodes"]))


def check_flags(n_nodes, eu, ev, oneway, state, shipped, comp, min_nodes) -> bool:
    """★統合後のグラフから強連結成分を数え直し、配った成分の番号・旗と突き合わせる。

    ★向きの規則は `router.worker.ts` の `rebuild()` と同じ:
      一方通行 … u → v のみ ／ それ以外 … 両方向。
    ★番号は「大きい順」なので、**同じ大きさの成分どうしは番号が入れ替わりうる**
      （ノードの番号づけが違うため）。番号そのものではなく**分け方**を比べる。
    """
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import connected_components

    ok = True
    for col, bit, name, keep in (
        (0, FLAG_MAIN, "除外なし", np.ones(len(eu), dtype=bool)),
        (1, FLAG_MAIN_NO_UNDERPASS, "アンダーパス除外", state != STATE_UNDERPASS),
    ):
        u, v, ow = eu[keep], ev[keep], oneway[keep].astype(bool)
        rows = np.concatenate([u, v[~ow]])
        cols = np.concatenate([v, u[~ow]])
        g = csr_matrix((np.ones(len(rows), dtype=np.int8), (rows, cols)),
                       shape=(n_nodes, n_nodes))
        _, labels = connected_components(g, directed=True, connection="strong")
        size = np.bincount(labels)[labels]
        got = comp[:, col]
        # (1) 番号が付くのは min_nodes 以上の成分に入るノードだけ
        bad_size = int(((got > 0) != (size >= min_nodes)).sum())
        # (2) 同じ番号 ⇔ 同じ成分（番号と成分が1対1）
        on = got > 0
        pairs = np.unique(np.c_[got[on], labels[on]], axis=0)
        bad_map = (len(pairs) - len(np.unique(pairs[:, 0]))) + \
            (len(pairs) - len(np.unique(pairs[:, 1])))
        # (3) 番号は大きい順（同じ大きさは入れ替わりうる）
        ranks = np.unique(got[on])
        sz = np.array([int((got == r).sum()) for r in ranks]) if len(ranks) < 5000 else None
        bad_order = int((np.diff(sz) > 0).sum()) if sz is not None else 0
        # (4) 旗は「番号が付いている」
        bad_flag = int((((shipped & bit) != 0) != on).sum())
        print(f"  {name:<18} 成分 {len(ranks):>6,} 個 / 大きさの食い違い {bad_size:,} / "
              f"分け方の食い違い {bad_map:,} / 順序 {bad_order:,} / 旗 {bad_flag:,}")
        if bad_size or bad_map or bad_order or bad_flag:
            ok = False
    return ok


def _weak_sizes(n: int, u, v) -> np.ndarray:
    """弱連結成分の大きさ（大きい順）。"""
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import connected_components

    a = csr_matrix((np.ones(len(u), dtype=np.int8), (np.asarray(u), np.asarray(v))),
                   shape=(n, n))
    _, lab = connected_components(a, directed=True, connection="weak")
    return np.sort(np.bincount(lab))[::-1]


def main() -> int:
    n_nodes, eu, ev, oneway, state, shipped, comp, min_nodes = merge(DATA)
    sizes = _weak_sizes(n_nodes, eu, ev)
    got = sizes[0] / n_nodes
    print(f"統合後  : ノード {n_nodes:,} / エッジ {len(eu):,} / "
          f"成分 {len(sizes)} / ★最大 {sizes[0]:,}（{got:.2%}）")

    # ★比べる元は**地域ごとの最終表**（書き出しの入力そのもの）。重なりは同じ規則で畳む。
    #   ★networkx は使わない。関東・全国では数千万ノードになりメモリに載らない。
    idx = json.loads((DATA / "index.json").read_text(encoding="utf-8"))
    names = idx.get("regions") or [idx["region"]]
    e0 = pd.concat([pd.read_parquet(FINAL / f"{n}.parquet", columns=["u", "v", "way"])
                    for n in names], ignore_index=True).drop_duplicates(["u", "v", "way"])
    print(f"（元は {FINAL}/ の {', '.join(names)}）")
    keys, inv = np.unique(np.concatenate([e0["u"].to_numpy(), e0["v"].to_numpy()]),
                          return_inverse=True)
    half = len(e0)
    sizes0 = _weak_sizes(len(keys), inv[:half], inv[half:])
    want = sizes0[0] / len(keys)
    print(f"元      : ノード {len(keys):,} / エッジ {len(e0):,} / "
          f"成分 {len(sizes0)} / ★最大 {sizes0[0]:,}（{want:.2%}）")

    ok = abs(got - want) < TOLERANCE
    print("\n" + ("★統合は正しい（最大成分の割合が一致）"
                  if ok else "★★分断されている。境界ノードの同一視を疑う"))
    print(f"  成分の大きさ 上位5: 統合後 {sizes[:5].tolist()}")
    print(f"                      元     {sizes0[:5].tolist()}")

    print("\n★巨大強連結成分の旗を検算する（→ kansui.connectivity）")
    flags_ok = check_flags(n_nodes, eu, ev, oneway, state, shipped, comp, min_nodes)
    print("  " + ("★旗は正しい" if flags_ok
                  else "★★旗が間違っている。到達不能な場所へ起終点を寄せる"))
    return 0 if (ok and flags_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
