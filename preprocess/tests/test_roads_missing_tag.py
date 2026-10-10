"""タグ列が無いとき: 「読み取りが効いていない」なら止め、「データに無い」なら空で足す。"""

from __future__ import annotations

import pandas as pd

from kansui import roads as R


class _OSM:
    def __init__(self, tagged_ids):
        self.tagged_ids = tagged_ids

    def get_data_by_custom_criteria(self, **kw):
        return pd.DataFrame({"id": self.tagged_ids}) if self.tagged_ids else None


def test_tag_on_extracted_road_means_extraction_is_broken():
    edges = pd.DataFrame({"id": [10, 11, 12]})
    assert R._tag_on_edges(_OSM([11]), "maxheight", edges)       # ★止める側


def test_tag_only_on_non_road_ways_is_absent():
    # 屋根付きの歩道のように、抽出されない way だけがタグを持つ（沖縄で実測）
    edges = pd.DataFrame({"id": [10, 11, 12]})
    assert not R._tag_on_edges(_OSM([99]), "covered", edges)


def test_no_tagged_way_at_all():
    assert not R._tag_on_edges(_OSM([]), "covered", pd.DataFrame({"id": [1]}))


def test_cannot_match_ids_falls_to_broken():
    """★突き合わせられないなら「効いていない」に倒す（推測で続行しない）。"""
    assert R._tag_on_edges(_OSM([1]), "covered", pd.DataFrame({"u": [1]}))
