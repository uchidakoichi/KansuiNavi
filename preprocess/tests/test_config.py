"""config.yaml が「未確定を推測で埋めない」仕組みとして機能しているかのテスト。

★守りたいのは、藤沢版 T-16 で実際に踏んだ形:
  YAML の `0466251111` が**8進数として 81351241 に化けた**。
  桁が全部 0〜7 だと起きる。「数字の並びか」を見るだけの検査は素通りする。
  → **型が str であることを必須にする。**
"""

from __future__ import annotations

import pytest
import yaml

from kansui.config import Config, ConfigError
from kansui.region import Region, RegionError


@pytest.fixture
def cfg() -> Config:
    return Config.load()


# -----------------------------------------------------------------------------
# ★未確定値は落ちる
# -----------------------------------------------------------------------------
def test_require_fails_on_unconfirmed_values(cfg):
    """`null` を require で読んだら落ちること。★既定値で通さない。"""
    for dotted in (
        "split.route_rings",          # P-2 で実測して決める
        "crs.straddling_mesh_policy",  # NK-08
        "dem.coverage_dem5a",         # NK-07
        "boundary.url",               # 全国版の URL が未確定
        "width.absolute_threshold_m",  # ★区分では答えられない
        "hazard.nationwide_layer",    # NK-14
    ):
        with pytest.raises(ConfigError):
            cfg.require(dotted)


def test_decided_values_are_present(cfg):
    """決まっているものは読めること。"""
    # ★2026-09-26 に 1.0 → 0.0（利用者判断）。同時接続1 は変えない。429/503 後は 1.0 に戻す
    assert cfg.require("tiles.sleep_sec") == 0.0
    assert cfg.require("tiles.sleep_after_throttle_sec") == 1.0
    assert cfg.require("tiles.max_concurrency") == 1
    assert cfg.require("split.level") == 2
    assert cfg.require("gsi_vector.zoom") == 16
    assert cfg.require("routing.lambda_default") == 8.0
    assert cfg.require("routing.calibrated_depth_m") == 1.407


# -----------------------------------------------------------------------------
# ★T-16: 都道府県コードが数値に化けていないこと
# -----------------------------------------------------------------------------
def test_prefecture_codes_are_strings_in_the_yaml(cfg):
    """★引用符が外れると `"01"` が 1 になり、先頭の 0 が落ちる。

    「数字か」だけを見る検査では通ってしまうので、**型**で止める。
    """
    for name, spec in cfg.require("regions").items():
        codes = spec["pref_codes"]
        assert isinstance(codes, list), name
        for c in codes:
            assert isinstance(c, str), f"{name}: 都道府県コードが文字列でない: {c!r}"
            assert len(c) == 2


def test_yaml_really_turns_unquoted_zero_prefixed_codes_into_numbers():
    """★この事故が本当に起きることを、テストの形で残しておく。"""
    loaded = yaml.safe_load('a: 01\nb: "01"\n')
    assert loaded["a"] == 1, "引用符が無いと 0 が落ちる"
    assert loaded["b"] == "01"


def test_region_refuses_a_numeric_prefecture_code():
    """config から作るときに型で止まること。"""
    with pytest.raises(RegionError):
        Region.from_config({"name": "hokkaido", "pref_codes": [1]})


def test_region_builds_from_the_shipped_config(cfg):
    """★同梱の config.yaml がそのまま Region になること。"""
    for name, spec in cfg.require("regions").items():
        r = Region.from_config({**spec, "name": name})
        assert r.name == name
    tk = cfg.require("regions")["tokyo_kanagawa"]
    assert tuple(tk["pref_codes"]) == ("13", "14")


def test_national_lists_only_known_regions(cfg):
    """★配布にまとめる地域は regions に定義があること（綴りの誤りで黙って抜けない）。"""
    known = set(cfg.require("regions"))
    for name in cfg.require("national.regions"):
        assert name in known, name


def test_each_prefecture_belongs_to_one_region(cfg):
    """★同じ区域を2つの地域に入れない。重なりは +3km の縁だけにする。

    県を系ごとに分けた地域（北海道など）は、市区町村の集合が互いに素であること。
    """
    whole: dict[str, str] = {}
    split: dict[str, dict[str, str]] = {}
    for name, spec in cfg.require("regions").items():
        munis = spec.get("municipality_codes")
        for c in spec["pref_codes"]:
            if munis is None:
                assert c not in whole and c not in split, f"県 {c} が重複（{name}）"
                whole[c] = name
            else:
                assert c not in whole, f"県 {c} が全域と分割の両方にある（{name}）"
                seen = split.setdefault(c, {})
                for m in munis:
                    if m[:2] == c:
                        assert m not in seen, f"市区町村 {m} が {seen[m]} と {name} の両方にある"
                        seen[m] = name


def test_all_47_prefectures_have_a_region(cfg):
    codes = {c for spec in cfg.require("regions").values() for c in spec["pref_codes"]}
    assert codes == {f"{i:02d}" for i in range(1, 48)}


def test_region_needs_an_explicit_name(cfg, monkeypatch):
    """★既定の地域を持たない。地域を取り違えたまま数時間の取得を流さない。"""
    monkeypatch.delenv("KANSUI_REGION", raising=False)
    with pytest.raises(RegionError):
        Region.from_env(cfg)
    monkeypatch.setenv("KANSUI_REGION", "no_such_region")
    with pytest.raises(RegionError):
        Region.from_env(cfg)
    monkeypatch.setenv("KANSUI_REGION", "saitama")
    assert Region.from_env(cfg).pref_codes == ("11",)


def test_shipped_region_excludes_tokyo_islands_and_unassigned_land(cfg):
    """★東京都を含めるなら島しょ部と所属未定地を外すこと。

    2026-09-23、所属未定地（13000）を外し忘れて範囲が**北緯 29.79度（孀婦岩）まで**伸びた。
    """
    r = Region.from_config({**cfg.require("regions")["tokyo_kanagawa"], "name": "tk"})
    ex = set(r.exclude_municipality_codes or ())
    assert "13000" in ex                       # 所属未定地
    assert {"13361", "13401", "13421"} <= ex   # 大島町・八丈町・小笠原村


# -----------------------------------------------------------------------------
# ★決定を config に残しておく（再議論しないため）
# -----------------------------------------------------------------------------
def test_vt_width_is_not_used(cfg):
    """★仕様書が「地図表示用の仮想的な幅員」と明記している。実測ではない。"""
    assert cfg.require("gsi_vector.use_rnkwidth") is True
    assert cfg.get("gsi_vector.use_vt_width") is False


def test_width_source_is_unified_for_routing(cfg):
    """★NK-18 (c): 経路計算は区分に統一、PLATEAU 実測は表示だけ。

    混ぜると市境で `μ·narrow` の効き方が変わり、不自然な迂回が出る。
    """
    assert cfg.require("width.routing_source") == "gsi_rnkwidth"
    assert cfg.require("width.display_source") == "plateau"


def test_hazard_is_display_only(cfg):
    """★w2 = 0。浸水想定はスコアに入れず重畳表示のみ（藤沢版の決定を継承）。"""
    assert cfg.get("risk.w2_hazard") == 0.0


def test_dem10b_is_used(cfg):
    """★NK-07 決定（2026-09-28・利用者判断「ないよりはまし」）: DEM10B を使い、区別して明示する。
    ★型が bool であること（YAML の no/yes の取り違えを防ぐ）。"""
    assert cfg.require("dem.exclude_dem10b") is False


def test_distance_crs_is_not_hardcoded(cfg):
    """★藤沢版は EPSG:6677 を直書きしていた。全国版は地域から引き当てる。"""
    text = (cfg.path).read_text(encoding="utf-8")
    assert "6677" not in text.split("# ---")[0], "region 節に系を直書きしない"
    assert cfg.get("crs.projected") is None, "★系は config に固定しない"
    assert cfg.require("crs.web_mercator") == "EPSG:3857"


def test_osm_tag_value_lists_are_strings(cfg):
    """★`no` / `yes` は YAML 1.1 で真偽値になる。引用符を外すと access=no が1本も落ちない。"""
    for key in ("osm.exclude_vehicle_access_values", "osm.exclude_highway_values",
                "osm.exclude_service_values"):
        for v in cfg.require(key):
            assert isinstance(v, str), f"{key} に文字列でない値: {v!r}"
