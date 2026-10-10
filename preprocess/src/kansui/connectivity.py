"""ノードが「巨大強連結成分」に属するかを前処理で判定し、配布物に入れる。

★★**なぜ要るか（2026-09-21 に実機で踏んだ）**

目的地が到達不能な場所（孤立した道路の断片）だと、A* は諦める前に
**地域のほぼ全域を読み込む。** 神奈川県・直線 8km での実測:

    経路が見つかりません
    読み込んだ区画   50 枚   ← ★全部で 56 枚
    通信量（累計）   16.48 MB
    確定ノード       1,293,073
    ★途中で読んだ区画 48 回

遅延読み込み（F-11）は「経路に要る区画だけ読む」ための仕組みなのに、
**最も効いてほしい場面で最悪の挙動**になる。利用者の通信量の事故でもある。

★藤沢版 Step 5 の決定は「**起終点を巨大強連結成分へ寄せる**」だった。
  ただし藤沢版はグラフを1本で全部持っていたので、クライアント側で成分を数えられた。
  全国版は区画を遅延読み込みするため、**読む前には成分が分からない。**
  → **前処理で答えを出して配る。**

★**強**連結で見る。弱連結だと「両端とも巨大成分」に見えて原因を取り違える
  （藤沢版 Step 5 で実証済み。2組がそれで誤診された）。

★**2通り計算する。** アンダーパスを除外すると連結性が変わるため
  （藤沢版の実測: 98.50% → 91.79%）。UI のチェックで答えが変わるので、
  **片方だけ配ると、チェックを入れた瞬間に嘘になる。**

★**向きの扱いは経路探索と1文字も違えないこと。** ここがずれると
  「つながっていると配ったのに、クライアントでは行けない」という
  **最も気づきにくい壊れ方**になる（→ `same_direction_rule` の注記）。
"""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger(__name__)

#: bit0 … 除外なしのときの巨大強連結成分に属する
FLAG_MAIN = 1
#: bit1 … アンダーパスを除外したときの巨大強連結成分に属する
FLAG_MAIN_NO_UNDERPASS = 2

#: `oneway` タグのうち「一方通行」と見なす値。★`export_mesh` / `router.worker.ts` と揃える
ONEWAY_TRUE = frozenset({"yes", "true", "1", "-1"})
#: ★`-1` は「幾何の向きと**逆**」の意味。`export_mesh.export` で u/v を入れ替えて渡す
ONEWAY_REVERSED = "-1"


def oneway_mask(values) -> np.ndarray:
    """`oneway` タグ → bool。★`-1` も一方通行（向きは呼び出し側で入れ替え済みとする）。"""
    import pandas as pd

    return pd.Series(values).astype(str).isin(ONEWAY_TRUE).to_numpy()


def reversed_mask(values) -> np.ndarray:
    """`oneway=-1`（幾何と逆向きの一方通行）の行。★u/v を入れ替える対象。"""
    import pandas as pd

    return (pd.Series(values).astype(str) == ONEWAY_REVERSED).to_numpy()


def _main_component(
    gu: np.ndarray, gv: np.ndarray, oneway: np.ndarray, n_nodes: int, keep: np.ndarray
) -> np.ndarray:
    """`keep` が True のエッジだけで強連結成分を取り、最大成分に属するノードを返す。

    ★**向きの規則**（`router.worker.ts` の `rebuild()` と同じにすること）:
      一方通行 … u → v のみ
      それ以外 … u → v と v → u の両方
    """
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import connected_components

    u = gu[keep]
    v = gv[keep]
    ow = oneway[keep]
    # 双方向のエッジは逆向きも張る
    idt = np.int32 if n_nodes < 2**31 else np.int64   # ★全国で数千万本。int64 にしない
    rows = np.concatenate([u, v[~ow]]).astype(idt, copy=False)
    cols = np.concatenate([v, u[~ow]]).astype(idt, copy=False)
    g = csr_matrix(
        (np.ones(len(rows), dtype=np.int8), (rows, cols)), shape=(n_nodes, n_nodes)
    )
    _, labels = connected_components(g, directed=True, connection="strong")
    counts = np.bincount(labels, minlength=1)
    main = int(np.argmax(counts))
    return labels == main


def node_flags(
    u_key: np.ndarray,
    v_key: np.ndarray,
    oneway: np.ndarray,
    underpass: np.ndarray,
) -> tuple[np.ndarray, dict]:
    """エッジの両端ノードに立てる旗を `(エッジ数, 2)` の uint8 で返す。

    `u_key` / `v_key` は OSM node ID（**座標ではない**。→ `export_mesh` の設計判断3）。
    `oneway` / `underpass` は bool。返り値の 2 列目は `v` 側。

    ★**判定不能（アンダーパス）のエッジを「除外あり」の側から落とす。**
      UI の「検出できたアンダーパスを除外する」と同じ意味。
    """
    keys = np.concatenate([u_key, v_key])
    uniq, inv = np.unique(keys, return_inverse=True)
    m = len(u_key)
    return node_flags_ids(inv[:m], inv[m:], len(uniq), oneway, underpass)


def node_flags_ids(
    gu: np.ndarray, gv: np.ndarray, n: int, oneway: np.ndarray, underpass: np.ndarray
) -> tuple[np.ndarray, dict]:
    """`node_flags` の、ノードを 0..n-1 の番号で渡す版（★全国で鍵の np.unique を二度しない）。"""
    m = len(gu)
    if len(gv) != m:
        raise ValueError("gu と gv の長さが違う")
    all_edges = np.ones(m, dtype=bool)
    main = _main_component(gu, gv, oneway, n, all_edges)
    main_no_up = _main_component(gu, gv, oneway, n, ~underpass)

    per_node = np.zeros(n, dtype=np.uint8)
    per_node[main] |= FLAG_MAIN
    per_node[main_no_up] |= FLAG_MAIN_NO_UNDERPASS

    stats = {
        "nodes": int(n),
        "main": int(main.sum()),
        "main_share": float(main.mean()),
        "main_no_underpass": int(main_no_up.sum()),
        "main_no_underpass_share": float(main_no_up.mean()),
        "underpass_edges": int(underpass.sum()),
    }
    log.info(
        "巨大強連結成分: 除外なし %s/%s (%.2f%%) / アンダーパス除外 %s (%.2f%%)",
        f"{stats['main']:,}",
        f"{n:,}",
        stats["main_share"] * 100,
        f"{stats['main_no_underpass']:,}",
        stats["main_no_underpass_share"] * 100,
    )
    return np.c_[per_node[gu], per_node[gv]].astype(np.uint8), stats


def component_ranks(
    gu: np.ndarray, gv: np.ndarray, oneway: np.ndarray, n_nodes: int, keep: np.ndarray,
    min_nodes: int,
) -> tuple[np.ndarray, np.ndarray]:
    """強連結成分に**大きい順の番号**を振る（1 = 最大）。`min_nodes` 未満の成分は 0。

    ★★全国では「最大の成分」が1つでは足りない（2026-09-23 のレビューで指摘）。
      北海道・沖縄・離島は本州と道路でつながらないので、最大の成分だけに旗を立てると
      **そこでは経路が一度も引けない**。→ 成分ごとに番号を配り、クライアントは
      「出発地と目的地が同じ番号の成分か」で判定する（違えば探索せずに断る）。

    返り値: (ノードごとの番号 uint16, 番号 1.. の成分の大きさ)
    """
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import connected_components

    idt = np.int32 if n_nodes < 2**31 else np.int64
    u, v, ow = gu[keep], gv[keep], oneway[keep]
    rows = np.concatenate([u, v[~ow]]).astype(idt, copy=False)
    cols = np.concatenate([v, u[~ow]]).astype(idt, copy=False)
    g = csr_matrix((np.ones(len(rows), dtype=np.int8), (rows, cols)), shape=(n_nodes, n_nodes))
    _, labels = connected_components(g, directed=True, connection="strong")
    sizes = np.bincount(labels)
    order = np.argsort(-sizes, kind="stable")          # 大きい順（同じ大きさは番号の順）
    big = int((sizes[order] >= min_nodes).sum())
    big = min(big, 65535)                              # uint16 に収める（0 は「小さい」）
    rank_of = np.zeros(len(sizes), dtype=np.uint16)
    rank_of[order[:big]] = np.arange(1, big + 1, dtype=np.uint16)
    return rank_of[labels], sizes[order[:big]]
