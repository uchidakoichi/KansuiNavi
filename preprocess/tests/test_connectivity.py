"""`kansui.connectivity` の検査。

★ここで固定したいのは「**向きの規則が経路探索と同じか**」の1点。
  ずれると「つながっていると配ったのに、クライアントでは行けない」という
  最も気づきにくい壊れ方になる（→ `connectivity` の冒頭コメント）。
"""

from __future__ import annotations

import numpy as np
import pytest

from kansui.connectivity import (
    FLAG_MAIN,
    FLAG_MAIN_NO_UNDERPASS,
    node_flags,
    oneway_mask,
    reversed_mask,
)


def _flags(u, v, oneway, underpass):
    return node_flags(
        np.array(u, dtype=np.float64),
        np.array(v, dtype=np.float64),
        np.array(oneway, dtype=bool),
        np.array(underpass, dtype=bool),
    )


def test_isolated_fragment_is_not_in_main():
    # 1-2-3-4 の双方向の鎖（4ノード）と、離れた 90-91 の断片（2ノード）
    u = [1, 2, 3, 90]
    v = [2, 3, 4, 91]
    flags, stats = _flags(u, v, [False] * 4, [False] * 4)
    assert stats["nodes"] == 6
    assert stats["main"] == 4
    # 鎖の3本のエッジは両端とも旗が立つ
    assert (flags[:3] & FLAG_MAIN).all()
    # ★断片は立たない
    assert not (flags[3] & FLAG_MAIN).any()


def test_oneway_dead_end_is_excluded():
    # 1⇄2⇄3 の双方向の輪ではない鎖 ＋ 3→9 の一方通行（9 からは戻れない）
    u = [1, 2, 3]
    v = [2, 3, 9]
    flags, stats = _flags(u, v, [False, False, True], [False] * 3)
    # ★9 は「入れるが出られない」ので**強**連結成分ではない
    assert stats["main"] == 3
    assert (flags[2, 0] & FLAG_MAIN) != 0      # 3 は巨大成分
    assert (flags[2, 1] & FLAG_MAIN) == 0      # ★9 は外


def test_oneway_cycle_is_strongly_connected():
    # 1→2→3→1 の環状一方通行。**弱連結でも強連結でもつながる**
    flags, stats = _flags([1, 2, 3], [2, 3, 1], [True] * 3, [False] * 3)
    assert stats["main"] == 3
    assert (flags & FLAG_MAIN).all()


def test_excluding_underpass_changes_connectivity():
    # 1-2 と 3-4 を つなぐ唯一の道 2-3 がアンダーパス
    u = [1, 2, 3]
    v = [2, 3, 4]
    underpass = [False, True, False]
    flags, stats = _flags(u, v, [False] * 3, underpass)
    # 除外なしなら全部つながる
    assert stats["main"] == 4
    assert (flags & FLAG_MAIN).all()
    # ★除外すると 2つの成分に割れ、大きいほうは同点で 2ノード
    assert stats["main_no_underpass"] == 2
    # 1-2 側が残り、3-4 側は落ちる（★「除外あり」の旗だけが消える）
    assert (flags[0] & FLAG_MAIN_NO_UNDERPASS).all()
    assert not (flags[2] & FLAG_MAIN_NO_UNDERPASS).any()
    # ★除外なしの旗は全ノードで立ったまま。2本のビットが独立していること
    assert (flags & FLAG_MAIN).all()


def test_flags_use_two_separate_bits():
    flags, _ = _flags([1], [2], [False], [False])
    assert (flags == (FLAG_MAIN | FLAG_MAIN_NO_UNDERPASS)).all()


@pytest.mark.parametrize(
    ("value", "expected"),
    [("yes", True), ("true", True), ("1", True), ("-1", True),
     ("no", False), ("reversible", False), ("alternating", False), (None, False)],
)
def test_oneway_mask_matches_router(value, expected):
    assert bool(oneway_mask([value])[0]) is expected


def test_reversed_mask_only_minus_one():
    assert list(reversed_mask(["yes", "-1", "no", None])) == [False, True, False, False]


def test_component_ranks_give_islands_their_own_number():
    """★本州（大）と島（小）の両方に番号が付き、小さすぎる断片は 0。"""
    import numpy as np

    from kansui.connectivity import component_ranks

    # 0-1-2 が本州（双方向）、3-4 が島（双方向）、5→6 は一方通行だけの断片
    gu = np.array([0, 1, 3, 5])
    gv = np.array([1, 2, 4, 6])
    ow = np.array([False, False, False, True])
    rank, sizes = component_ranks(gu, gv, ow, 7, np.ones(4, bool), min_nodes=2)
    assert rank[[0, 1, 2]].tolist() == [1, 1, 1]
    assert rank[[3, 4]].tolist() == [2, 2]
    assert rank[[5, 6]].tolist() == [0, 0]       # 強連結でない（1ノードずつ）
    assert sizes.tolist() == [3, 2]
    rank3, _ = component_ranks(gu, gv, ow, 7, np.ones(4, bool), min_nodes=3)
    assert rank3[[3, 4]].tolist() == [0, 0]      # 島が小さすぎれば番号なし
