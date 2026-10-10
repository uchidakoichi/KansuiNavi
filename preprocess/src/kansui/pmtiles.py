"""手元に置いた PMTiles（v3）からタイルを引く。**通信しない。**

★国土地理院の最適化ベクトルタイルは、XYZ 版（`optimal_bvmap-v1/{z}/{x}/{y}.pbf`）が
  **2023年10月1日時点で更新停止**、PMTiles 版が **3か月ごとに更新**されている
  （https://github.com/gsi-cyberjapan/optimal_bvmap 、2026-09-25 確認。
   2026-09-17 に 2026年7月1日時点へ更新）。
  → 全国ファイル（約16.9GB）を**1回の取得**で手元に置き、以後はここから引く。
    XYZ 版を1枚ずつ取ると全国で約170万回のリクエストになり、しかもデータが古い。

形式は PMTiles v3（https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md）。
依存を足さないため自前で読む。★読めるのは内部圧縮 gzip・タイル圧縮 gzip/none のものだけ。
"""

from __future__ import annotations

import gzip
import mmap
import struct
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

MAGIC = b"PMTiles"
HEADER_LEN = 127
_COMP_NONE, _COMP_GZIP = 1, 2


class PMTilesError(RuntimeError):
    pass


def zxy_to_tileid(z: int, x: int, y: int) -> int:
    """(z, x, y) → PMTiles のタイル ID（ズームごとのヒルベルト曲線順）。"""
    if not (0 <= x < (1 << z) and 0 <= y < (1 << z)):
        raise ValueError(f"範囲外のタイル: {z}/{x}/{y}")
    acc = ((1 << (2 * z)) - 1) // 3  # それより低いズームのタイル数の合計
    n = 1 << z
    s = n >> 1
    d = 0
    while s > 0:
        rx = 1 if x & s else 0
        ry = 1 if y & s else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x = n - 1 - x
                y = n - 1 - y
            x, y = y, x
        s >>= 1
    return acc + d


def _varints(buf: bytes):
    i, n = 0, len(buf)
    while i < n:
        v = shift = 0
        while True:
            b = buf[i]
            i += 1
            v |= (b & 0x7F) << shift
            if b < 0x80:
                break
            shift += 7
        yield v


@dataclass
class _Dir:
    ids: list[int]
    runs: list[int]
    lens: list[int]
    offs: list[int]


def _parse_dir(raw: bytes) -> _Dir:
    it = _varints(raw)
    n = next(it)
    ids, last = [], 0
    for _ in range(n):
        last += next(it)
        ids.append(last)
    runs = [next(it) for _ in range(n)]
    lens = [next(it) for _ in range(n)]
    offs = []
    for i in range(n):
        v = next(it)
        # ★0 は「直前の続き」（直前の offset + length）。ただし先頭は本当に 0
        offs.append(offs[i - 1] + lens[i - 1] if v == 0 and i > 0 else v - 1 if v else 0)
    return _Dir(ids, runs, lens, offs)


class PMTiles:
    """`fetch(z, x, y)` は MVT のバイト列（展開済み）か、タイルが無ければ None。

    `kansui.gsi_vector.VectorTiles.fetch` と同じ形で使える。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._f = open(self.path, "rb")  # noqa: SIM115
        self._mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        h = self._mm[:HEADER_LEN]
        if h[:7] != MAGIC or h[7] != 3:
            raise PMTilesError(f"PMTiles v3 ではない: {self.path}")
        f = struct.unpack("<11Q6B4iB2i", h[8:HEADER_LEN])
        (self.root_off, self.root_len, _mo, _ml, self.leaf_off, _ll,
         self.data_off, self.data_len, _na, _ne, _nc) = f[:11]
        self.internal_comp, self.tile_comp, self.tile_type = f[12], f[13], f[14]
        self.min_zoom, self.max_zoom = f[15], f[16]
        if self.internal_comp != _COMP_GZIP or self.tile_comp not in (_COMP_NONE, _COMP_GZIP):
            raise PMTilesError(f"未対応の圧縮: 内部 {self.internal_comp} / タイル {self.tile_comp}")
        # ★ファイルが途中で切れていないか（取得の中断）。末尾のタイルが読めないと静かに欠ける
        if len(self._mm) < self.data_off + self.data_len:
            raise PMTilesError(f"ファイルが短い（取得が途中で止まった？）: {len(self._mm):,} < "
                               f"{self.data_off + self.data_len:,}")
        self._root = self._read_dir(self.root_off, self.root_len)
        self._leaves: dict[int, _Dir] = {}
        self.stats = {"hits": 0, "missing": 0}

    def _read_dir(self, off: int, length: int) -> _Dir:
        return _parse_dir(gzip.decompress(self._mm[off:off + length]))

    def _find(self, tid: int) -> tuple[int, int] | None:
        d = self._root
        for _ in range(4):  # 仕様上、ルート＋葉は最大 3 段
            i = bisect_right(d.ids, tid) - 1
            if i < 0:
                return None
            if d.runs[i] == 0:  # 葉ディレクトリへの参照
                key = d.offs[i]
                leaf = self._leaves.get(key)
                if leaf is None:
                    if len(self._leaves) > 4096:
                        self._leaves.clear()
                    leaf = self._leaves[key] = self._read_dir(self.leaf_off + key, d.lens[i])
                d = leaf
                continue
            if tid < d.ids[i] + d.runs[i]:
                return d.offs[i], d.lens[i]
            return None
        raise PMTilesError("ディレクトリが深すぎる")

    def fetch(self, z: int, x: int, y: int) -> bytes | None:
        hit = self._find(zxy_to_tileid(z, x, y))
        if hit is None:
            self.stats["missing"] += 1
            return None
        off, length = hit
        raw = self._mm[self.data_off + off:self.data_off + off + length]
        self.stats["hits"] += 1
        return gzip.decompress(raw) if self.tile_comp == _COMP_GZIP else bytes(raw)

    def close(self) -> None:
        self._mm.close()
        self._f.close()
