"""速度の与え方（kansui.roads.assign_speeds）。"""

from __future__ import annotations

import pandas as pd

from kansui.config import Config
from kansui.roads import assign_speeds


def test_absurd_maxspeed_falls_back_to_the_default():
    """★北海道の maxspeed=560 の誤記1件で、全国の最高速度が 4.7倍になるところだった。"""
    cfg = Config.load()
    e = pd.DataFrame({"maxspeed": ["560", "1", "40", None],
                      "highway": ["secondary"] * 4, "length": [100.0] * 4})
    out = assign_speeds(e, cfg)
    default = float(cfg.require("osm.default_speed_kmh").get("secondary",
                    cfg.require("osm.default_speed_kmh")["_other"]))
    assert out["speed_kmh"].tolist() == [default, default, 40.0, default]
    assert out["speed_source"].tolist() == ["default", "default", "maxspeed", "default"]
