"""PMTiles v3 の読み取り。★仕様の例と、自前で組んだ小さなアーカイブで往復を確かめる。"""

from __future__ import annotations

import gzip
import struct

import pytest

from kansui import pmtiles as P


def test_tileid_matches_spec_examples():
    # 仕様 v3: z0 は 0、z1 はヒルベルト順に (0,0)=1 (0,1)=2 (1,1)=3 (1,0)=4
    assert P.zxy_to_tileid(0, 0, 0) == 0
    assert [P.zxy_to_tileid(1, x, y) for x, y in [(0, 0), (0, 1), (1, 1), (1, 0)]] == [1, 2, 3, 4]
    # z2 の先頭は 5（z0 の1枚 + z1 の4枚）、全 16 枚が重複なく 5〜20 に入る
    ids = {P.zxy_to_tileid(2, x, y) for x in range(4) for y in range(4)}
    assert ids == set(range(5, 21))


def test_tileid_is_a_bijection_at_z6():
    n = 1 << 6
    base = ((1 << 12) - 1) // 3
    ids = {P.zxy_to_tileid(6, x, y) for x in range(n) for y in range(n)}
    assert ids == set(range(base, base + n * n))


def test_out_of_range_is_refused():
    with pytest.raises(ValueError):
        P.zxy_to_tileid(1, 2, 0)


def _varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _dir(entries):
    """entries: [(tile_id, offset, length, run_length)]（仕様の直列化。offset は連続なら 0）"""
    b = _varint(len(entries))
    last = 0
    for tid, *_ in entries:
        b += _varint(tid - last)
        last = tid
    for *_, run in entries:
        b += _varint(run)
    for _, _, ln, _ in entries:
        b += _varint(ln)
    prev = None
    for tid, off, ln, _ in entries:
        b += _varint(0 if prev and off == prev[0] + prev[1] else off + 1)
        prev = (off, ln)
    return gzip.compress(b)


def _archive(tmp_path, tiles: dict, *, leaf: bool = False, truncate: int = 0):
    """tiles: {(z,x,y): bytes}。★leaf=True のときは葉ディレクトリを1段挟む。"""
    data = b""
    entries = []
    for (z, x, y), body in sorted(tiles.items(), key=lambda kv: P.zxy_to_tileid(*kv[0])):
        g = gzip.compress(body)
        entries.append((P.zxy_to_tileid(z, x, y), len(data), len(g), 1))
        data += g
    if leaf:
        leaf_dir = _dir(entries)
        root = _dir([(entries[0][0], 0, len(leaf_dir), 0)])
    else:
        leaf_dir = b""
        root = _dir(entries)
    root_off = P.HEADER_LEN
    leaf_off = root_off + len(root)
    data_off = leaf_off + len(leaf_dir)
    hdr = P.MAGIC + bytes([3]) + struct.pack(
        "<11Q6B4iB2i", root_off, len(root), 0, 0, leaf_off, len(leaf_dir), data_off, len(data),
        len(tiles), len(tiles), len(tiles), 1, 2, 2, 1, 0, 16, 0, 0, 0, 0, 0, 0, 0)
    blob = hdr + root + leaf_dir + data
    p = tmp_path / "t.pmtiles"
    p.write_bytes(blob[:len(blob) - truncate])
    return p


@pytest.mark.parametrize("leaf", [False, True])
def test_roundtrip(tmp_path, leaf):
    tiles = {(16, 58373, 25736): b"road-a", (16, 58374, 25736): b"road-b", (1, 1, 0): b"low"}
    pm = P.PMTiles(_archive(tmp_path, tiles, leaf=leaf))
    for k, v in tiles.items():
        assert pm.fetch(*k) == v
    # ★無いタイルは None（XYZ の 404 と同じ扱い）
    assert pm.fetch(16, 0, 0) is None
    assert pm.stats == {"hits": 3, "missing": 1}


def test_truncated_file_is_refused(tmp_path):
    """★取得が途中で止まったファイルを黙って読まない（末尾のタイルが静かに欠ける）。"""
    with pytest.raises(P.PMTilesError):
        P.PMTiles(_archive(tmp_path, {(1, 0, 0): b"x" * 100}, truncate=5))
