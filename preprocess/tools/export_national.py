"""地域ごとの最終表をまとめて、配布データ（区画ごとの `.bin.gz` と `index.json`）を書き出す。

    uv run python tools/export_national.py

★★**全体で1つに決まる値はここで決める。** 地域ごとには決めない:
  - 巨大強連結成分の旗（`nodeFlags`）… 県境をまたいでつながるかは全体を見ないと分からない
  - 最高速度（A* のヒューリスティックの分母）… 地域ごとに違うと許容性が崩れる
  - 対象範囲（`cover3`）… 地図の破線とワーカーの「範囲外」判定の元

★同じエッジは地域の +3km の重なりで**複数の最終表に入る**。
  - 他の地域の内側にあるバッファの行は落とす（その地域が持ち主）
  - 残った重なり（どの地域の内側でもない行）は OSM の区間（`u`・`v`・`way`）で畳み、
    `national.regions` の先勝ち

★★**全国（約5,000万本）でも 16GB に収めるため、全部を一度にメモリへ載せない**（2026-09-23）。
  以前は全列を1つの表にしていて、合成 1,247万本でピーク 4.78GB、全国では 14〜15GB の見込みだった。
  → 全体で決める値は**軽い配列だけ**で決め（ノード番号・一方通行・判定不能・座標の補正）、
    区画の書き出しは**1次メッシュのまとまりごとに最終表を読み直して**流す。
  ★出力は以前の作りとバイト単位で同じであること（回帰で確かめる）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from kansui import connectivity as C
from kansui import export_mesh as EM
from kansui import mesh as MESH
from kansui.config import Config

FINAL = Path("data/raw/final")
ROADS = Path("data/raw/roads")
ELEV = Path("data/raw/elev")
OUT = Path("../web/public/data")

#: 書き出しで読む列（★要らない列は読まない）
COLUMNS = ["u", "v", "way", "lon0", "lat0", "lon1", "lat1", "travel_time_s", "oneway",
           "name", "ref", "s_topo", "dem_low", "state", "narrow", "inside"]


@dataclass
class Reg:
    """1地域の、全体で決める段階で持っておく軽い情報。"""

    name: str
    rank: int
    path: Path
    keep: np.ndarray                      # 元の行ごと: 配布に入れるか
    iu: np.ndarray = field(default=None)  # 入れる行の u / v のノード番号（全体の番号）
    iv: np.ndarray = field(default=None)
    mf: np.ndarray = field(default=None)  # 入れる行の両端の2次区画コード（揃えた座標から）
    mt: np.ndarray = field(default=None)
    #: 元の行ごと: 直した通過時間（NaN = 最終表のまま）
    tt_fix: np.ndarray = field(default=None)
    #: 元の行ごと: 橋の区間か（F-03）
    bridge: np.ndarray = field(default=None)
    #: 元の行ごと: 道路冠水想定箇所の確定の点に結び付いた区間か（NK-09）
    known: np.ndarray = field(default=None)

    def tt(self, df_tt: np.ndarray, sel: np.ndarray | None = None) -> np.ndarray:
        """入れる行（`sel` があればその部分）の通過時間。★直したものがあれば差し替える。"""
        fix = self.tt_fix[self.keep] if self.tt_fix is not None else None
        if fix is None:
            return df_tt
        if sel is not None:
            fix = fix[sel]
        return np.where(np.isfinite(fix), fix, df_tt).astype(df_tt.dtype)


def region_polygon(cfg, name: str):
    """その地域の区域（build_region の `inside` と同じ形）。N03 はキャッシュから読む。"""
    from kansui import boundary as B
    from kansui.fetcher import TileFetcher
    from kansui.region import Region

    spec = {**cfg.require("regions")[name], "name": name}
    g = B.load(TileFetcher.from_config(cfg), cfg.require("boundary.url_template"),
               Region.from_config(spec), cfg.resolve_path("boundary.cache_dir"))
    return g.to_crs(4326).union_all()


def _read(reg: Reg, columns: list[str]) -> pd.DataFrame:
    """配布に入れる行だけを、元の行の順で読む。"""
    df = pd.read_parquet(reg.path, columns=columns)
    return df.loc[reg.keep].reset_index(drop=True)


def road_fixes(cfg, name: str,
               final: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """道路抽出の表を引き当て、(自動車が通れない行, 直した通過時間 or NaN, 橋の行) を返す。

    ★最終表にはタグが無いので、書き出しの時点で道路抽出の表から判定する。
      こうすると**作成済みの地域にも効く**（作り直さずに済む）。
    ★行は最終表と同じ順（build_region は並べ替えも間引きもしない）。
      念のため両端のノードで突き合わせ、ずれていたら落とす（逆向き一方通行は入れ替え済み）。
    """
    import json as _json

    import pyarrow.parquet as pq

    roads = ROADS / f"edges_{name}.parquet"
    # ★タグの列は、その地域に1本も無ければ表に無い（千葉に service 列が無かった）。無い列は空
    have = set(pq.read_schema(roads).names)
    cols = ["u", "v", "highway", "access", "motor_vehicle", "motorcar", "tags",
            "maxspeed", "speed_kmh", "speed_source", "length", "bridge", "tunnel"]
    e = pd.read_parquet(roads, columns=[c for c in cols if c in have])
    for c in cols:
        if c not in e.columns:
            e[c] = pd.Series(pd.NA, index=e.index, dtype="string")
    if len(e) != len(final):
        raise SystemExit(f"★{name}: 最終表と道路抽出の表の行数が違う（{len(final)} / {len(e)}）")
    fu, fv = final["u"].to_numpy(), final["v"].to_numpy()
    eu, ev = e["u"].to_numpy(np.float64), e["v"].to_numpy(np.float64)
    ok = ((fu == eu) & (fv == ev)) | ((fu == ev) & (fv == eu))
    if not ok.all():
        raise SystemExit(f"★{name}: 最終表と道路抽出の表の行が対応しない（{int((~ok).sum())} 行）")

    # --- 自動車が通れない行 -------------------------------------------------------
    # OSM の通行規制の階層（具体的なものが優先）: motorcar → motor_vehicle → vehicle → access。
    # ★vehicle は pyrosm が列にせず tags（JSON）に入れる。2026-09-24 のレビューで抜けていたと判明
    #   （桜沢林道 vehicle=private・日向林道 vehicle=forestry などが残っていた）。
    vehicle = pd.Series(pd.NA, index=e.index, dtype="string")
    tags = e["tags"].astype("string")
    hit = tags.str.contains('"vehicle"', regex=False, na=False)
    if hit.any():
        vehicle[hit] = [(_json.loads(t) or {}).get("vehicle") for t in tags[hit]]
    eff = e["motorcar"].astype("string")
    for nxt in (e["motor_vehicle"].astype("string"), vehicle, e["access"].astype("string")):
        eff = eff.where(eff.notna(), nxt)
    closed = eff.isin([str(x) for x in cfg.require("osm.exclude_vehicle_access_values")])
    closed |= e["highway"].isin([str(x) for x in cfg.require("osm.exclude_highway_values")])

    # --- maxspeed の誤記を直した通過時間 ----------------------------------------
    # ★roads.assign_speeds の有効範囲（osm.maxspeed_valid_kmh）は道路抽出でしか効かない。
    #   関東はその前に作ったので、maxspeed=1〜4 の誤記（青梅街道など 95 区間）が残っていた
    #   （2026-09-24 のレビュー）。ここで同じ規則を当て直す（範囲外 → 道路種別の既定速度）。
    lo, hi = (float(x) for x in cfg.require("osm.maxspeed_valid_kmh"))
    sp = pd.to_numeric(e["speed_kmh"], errors="coerce")
    bad = (e["speed_source"].astype("string") == "maxspeed").fillna(False) & ((sp < lo) | (sp > hi))
    tt = np.full(len(e), np.nan, dtype=np.float32)
    if bad.any():
        defaults = cfg.require("osm.default_speed_kmh")
        fb = float(defaults["_other"])
        v = e.loc[bad, "highway"].map(lambda h: float(defaults.get(h, fb))).to_numpy(np.float32)
        ln = pd.to_numeric(e.loc[bad, "length"]).to_numpy()
        tt[bad.to_numpy()] = (ln / (v / 3.6)).astype(np.float32)
    # --- 橋・トンネル（F-03）: プロファイルでは両端を結ぶ ----------------------------------
    #   橋 … 標高タイルは橋の下の地面を測る ／ トンネル … 地表（上の山）を測る（2026-09-29）
    #   ★値: 0 なし / 1 橋 / 2 トンネル（edgeBridge の列のまま。形式は変えない）
    br = e["bridge"].astype("string")
    code = np.where((br.notna() & (br != "no")).to_numpy(bool), 1, 0).astype(np.uint8)
    if "tunnel" in e.columns:
        tv = [str(v) for v in cfg.require("osm.profile_tunnel_values")]
        code[e["tunnel"].astype("string").isin(tv).fillna(False).to_numpy(bool)] = 2
    return closed.to_numpy(bool), tt, code


# -----------------------------------------------------------------------------
# 1. 入れる行を決める（他の地域の内側のバッファを落とす → 重なりを畳む）
# -----------------------------------------------------------------------------
def select_rows(names: list[str], cfg) -> tuple[list[Reg], dict]:
    import shapely

    polys = {}
    if len(names) > 1:
        for n in names:
            polys[n] = region_polygon(cfg, n)
            shapely.prepare(polys[n])

    regs: list[Reg] = []
    light: list[dict] = []
    for rank, name in enumerate(names):
        p = FINAL / f"{name}.parquet"
        if not p.exists():
            raise SystemExit(
                f"★{p} が無い。先に作ること:\n"
                f"  KANSUI_REGION={name} uv run python tools/build_region.py"
            )
        df = pd.read_parquet(p, columns=["u", "v", "way", "lon0", "lat0", "inside", "state"])
        if (df["state"] == "known").any():
            raise SystemExit(f"★{name}: 最終表に state=known がある（NK-09 は書き出しで付ける）")
        n0 = len(df)
        inside = df["inside"].to_numpy(bool)
        closed, tt_fix, bridge = road_fixes(cfg, name, df)
        # ★★他の地域の内側にあるバッファの行は落とす（その地域が持ち主）。
        #   PBF の日付が違う地域どうしでは、古い側のバッファに**新しい側で消えた・
        #   向きの変わった・分割された区間**が残り、鍵が一致しないので畳めない
        #   （2026-09-23 のレビューで指摘）。
        #   持ち主の地域は、始点が内側にある区間を**すべて**持っているので、落としても欠けない。
        #   ★どの地域の内側でもない行（海上の橋・まだ作っていない隣県）は残す。
        keep = ~closed
        cand = np.flatnonzero(~inside & keep)
        others = [g for k, g in polys.items() if k != name]
        if len(cand) and others:
            x, y = df["lon0"].to_numpy()[cand], df["lat0"].to_numpy()[cand]
            owned = np.zeros(len(cand), dtype=bool)
            for poly in others:
                owned |= shapely.contains_xy(poly, x, y)
            keep[cand[owned]] = False
        n_tt = int(np.isfinite(tt_fix).sum())
        if n_tt:
            print(f"  ★{name}: maxspeed の誤記を既定速度で直した区間 {n_tt:,}", flush=True)
        print(f"  {name:<16} {n0:>10,} 本（うち始点が内側 {int(inside.sum()):,}）"
              f" / 自動車が通れない区間 {int(closed.sum()):,} 本"
              f" / 他の地域の内側のバッファ {n0 - int(keep.sum()) - int(closed.sum()):,} 本"
              " を落とした",
              flush=True)
        known = road_kansui_known(cfg, name, p, keep, bridge)
        regs.append(Reg(name=name, rank=rank, path=p, keep=keep, tt_fix=tt_fix, bridge=bridge,
                        known=known))
        light.append({"u": df["u"].to_numpy(np.float64)[keep],
                      "v": df["v"].to_numpy(np.float64)[keep],
                      "way": df["way"].to_numpy(np.int64)[keep],
                      "inside": inside[keep], "orig": np.flatnonzero(keep)})
        del df

    # ★重なりを畳む。上で他の地域のバッファを落としたので、重複が残りうるのは
    #   **始点が同じノード**の行どうしだけ。候補をそこに絞ってから並べる
    m0 = sum(len(d["u"]) for d in light)
    u_out = np.unique(np.concatenate([d["u"][~d["inside"]] for d in light]))
    cu, cv, cw, cin, crk, crow = [], [], [], [], [], []
    for r, d in zip(regs, light, strict=True):
        sel = np.flatnonzero(np.isin(d["u"], u_out))
        cu.append(d["u"][sel])
        cv.append(d["v"][sel])
        cw.append(d["way"][sel])
        cin.append(d["inside"][sel])
        crk.append(np.full(len(sel), r.rank, np.int32))
        crow.append(d["orig"][sel])
    cu, cv, cw = np.concatenate(cu), np.concatenate(cv), np.concatenate(cw)
    cin, crk, crow = np.concatenate(cin), np.concatenate(crk), np.concatenate(crow)
    o = np.lexsort((crow, crk, ~cin, cw, cv, cu))
    same = (cu[o][1:] == cu[o][:-1]) & (cv[o][1:] == cv[o][:-1]) & (cw[o][1:] == cw[o][:-1])
    drop = o[1:][same]
    for i in drop:
        regs[crk[i]].keep[crow[i]] = False
    for r, d in zip(regs, light, strict=True):
        k = r.keep[d["orig"]]
        for f in ("u", "v"):
            d[f] = d[f][k]
        for f in ("way", "inside", "orig"):
            del d[f]
    m = sum(len(d["u"]) for d in light)
    print(f"  ★重なりを畳んだ: {m0:,} → {m:,} 本（{m0 - m:,} 本が重複）", flush=True)

    # ★ノードの番号づけは**1回だけ**
    uniq, inv = np.unique(np.concatenate([np.concatenate([d["u"], d["v"]]) for d in light]),
                          return_inverse=True)
    inv = inv.astype(np.int32 if len(uniq) < 2**31 else np.int64)
    pos = 0
    for r, d in zip(regs, light, strict=True):
        n = len(d["u"])
        r.iu, r.iv = inv[pos:pos + n], inv[pos + n:pos + 2 * n]
        pos += 2 * n
    return regs, {"nodes": len(uniq)}


# -----------------------------------------------------------------------------
# 2. 地方の境目の座標の食い違いを揃える
# -----------------------------------------------------------------------------
def resolve_conflicts(node, x, y, inside, rank, row, side, pbf_of_rank):
    """同じノードの端点どうしで座標が違うものを、優先順の先頭の座標に揃える補正を返す。

    返り値: (直すノード番号の昇順配列, その x, その y)。★食い違いが無ければ空。
    ★正は優先順の先頭: 始点が内側 → 地域の順 → 行の順 → u 側が先。
    ★同じ PBF の地域どうしで食い違ったら落とす（向きの入れ替えや取り違えの誤り）。
    """
    empty = (np.array([], dtype=np.int64), np.array([]), np.array([]))
    if not len(node):
        return empty
    o = np.lexsort((side, row, rank, ~inside, node))
    node, x, y, rank = node[o], x[o], y[o], rank[o]
    head = np.r_[True, node[1:] != node[:-1]]
    ci = np.flatnonzero(head)[np.cumsum(head) - 1]
    moved = (x != x[ci]) | (y != y[ci])
    if not moved.any():
        return empty
    pb = np.array(pbf_of_rank, dtype=object)
    same_pbf = pb[rank[moved]] == pb[rank[ci][moved]]
    if same_pbf.any():
        raise SystemExit(f"★同じ PBF から作った地域どうしでノードの座標が違う: "
                         f"{int(same_pbf.sum())} 件（向きの入れ替えや取り違えを疑う）")
    d = np.hypot((x[moved] - x[ci][moved]) * 111320 * np.cos(np.radians(y[moved])),
                 (y[moved] - y[ci][moved]) * 110540)
    print(f"★PBF の日付が違う地域の境目で座標を揃えた: {int(moved.sum()):,} 端点 "
          f"（ずれ 中央値 {np.median(d):.2f}m / 最大 {d.max():.1f}m）", flush=True)
    fix = np.unique(node[moved])
    first = np.flatnonzero(head)
    at = np.searchsorted(node[first], fix)
    return fix, x[first][at], y[first][at]


def harmonize(regs: list[Reg], n_nodes: int, pbf_of_rank: list[str]):
    # 2つ以上の地域に現れるノードだけが食い違いうる
    cnt = np.zeros(n_nodes, dtype=np.int32)
    for r in regs:
        cnt[np.unique(np.concatenate([r.iu, r.iv]))] += 1
    shared = cnt > 1
    del cnt
    parts = {k: [] for k in ("node", "x", "y", "inside", "rank", "row", "side")}
    for r in regs:
        pick = shared[r.iu] | shared[r.iv]
        if not pick.any():
            continue
        df = _read(r, ["lon0", "lat0", "lon1", "lat1", "inside"])
        rows = np.flatnonzero(pick)
        for side, (ids, lx, ly) in enumerate(((r.iu, "lon0", "lat0"), (r.iv, "lon1", "lat1"))):
            sel = rows[shared[ids[rows]]]
            parts["node"].append(ids[sel].astype(np.int64))
            parts["x"].append(df[lx].to_numpy()[sel])
            parts["y"].append(df[ly].to_numpy()[sel])
            parts["inside"].append(df["inside"].to_numpy(bool)[sel])
            parts["rank"].append(np.full(len(sel), r.rank, np.int32))
            parts["row"].append(sel.astype(np.int64))
            parts["side"].append(np.full(len(sel), side, np.int8))
    if not parts["node"]:
        return resolve_conflicts(*(np.array([]) for _ in range(7)), pbf_of_rank)
    return resolve_conflicts(*(np.concatenate(parts[k]) for k in
                               ("node", "x", "y", "inside", "rank", "row", "side")), pbf_of_rank)


def _coords(r: Reg, df: pd.DataFrame, fix):
    """揃えた座標（(行, 2) の lon / lat）。"""
    # ★★必ず複製する。pandas 3 はコピーオンライトが既定で、float64 の列の to_numpy は
    #   **書き込めないビュー**を返す。補正を書き込むところで ValueError になり、PBF の日付が違う
    #   地域を混ぜた書き出しが必ず落ちていた（2026-09-23 のレビューで指摘。同じ PBF では通らない）
    lon = np.array(df[["lon0", "lon1"]].to_numpy(np.float64), copy=True)
    lat = np.array(df[["lat0", "lat1"]].to_numpy(np.float64), copy=True)
    fn, fx, fy = fix
    if len(fn):
        for side, ids in enumerate((r.iu, r.iv)):
            at = np.searchsorted(fn, ids)
            hit = (at < len(fn)) & (fn[np.minimum(at, len(fn) - 1)] == ids)
            lon[hit, side] = fx[at[hit]]
            lat[hit, side] = fy[at[hit]]
    return lon, lat


# -----------------------------------------------------------------------------
# NK-09 道路冠水想定箇所
# -----------------------------------------------------------------------------
def _road_kansui_points(cfg) -> pd.DataFrame:
    p = cfg.resolve_path("road_kansui.points_csv") if cfg is not None else None
    if p is None or not p.exists():
        return pd.DataFrame(columns=["region", "tier"])
    # ★空欄を NaN にしない。NaN は JSON に書くと不正な値になり、画面が**黙って**読めなくなる（2026-10-03 に踏んだ）
    return pd.read_csv(p, dtype=str, keep_default_na=False)


def road_kansui_known(cfg, name: str, path: Path, keep: np.ndarray, bridge) -> np.ndarray:
    """元の行ごとの known の印。★確定の点だけ。結び付けられなかった点は write_road_kansui が uncertain に落とす。"""
    from kansui import road_kansui as RK

    pts = _road_kansui_points(cfg)
    pts = pts[(pts["region"] == name) & (pts["tier"] == "confirmed")]
    if not len(pts):
        return np.zeros(len(keep), bool)
    df = pd.read_parquet(path, columns=["lon0", "lat0", "lon1", "lat1", "name", "ref"])
    is_bridge = (np.asarray(bridge) == 1) if bridge is not None else np.zeros(len(df), bool)
    nm = df["name"].astype("string")
    label = nm.where(nm.notna() & (nm != ""), df["ref"].astype("string"))
    known, snapped = RK.known_mask(pts, df["lon0"], df["lat0"], df["lon1"], df["lat1"], label,
                                   keep & ~is_bridge,
                                   float(cfg.require("road_kansui.snap_m")),
                                   float(cfg.require("road_kansui.same_label_m")))
    _SNAPPED.update(dict(zip(pts["id"], snapped)))
    print(f"  ★NK-09 {name}: 確定 {len(pts)} 点のうち {sum(snapped)} 点を区間に結び付けた"
          f"（区間 {int(known.sum())} 本）", flush=True)
    return known


_SNAPPED: dict = {}


def write_road_kansui(cfg, out_dir: Path, names: list[str]) -> int:
    """road_kansui.json を書き、件数を返す。★結び付けられなかった確定の点は uncertain（表示だけ）に落とす。"""
    from kansui import road_kansui as RK

    pts = _road_kansui_points(cfg)
    # ★地域の無い行（座標が公表されていない「位置不明」・どの地域にも入らない点）も表示のために残す
    pts = pts[pts["region"].isin(names) | (pts["region"] == "")].copy()
    if not len(pts):
        return 0
    lost = pts["tier"].eq("confirmed") & ~pts["id"].map(_SNAPPED).fillna(False).astype(bool)
    pts["radius_m"] = pts["radius_m"].replace("", np.nan).astype(float)
    if lost.any():
        print(f"  ★NK-09: 区間に結び付けられなかった確定の点 {int(lost.sum())} → 要確認（表示だけ）に落とす: "
              f"{list(pts.loc[lost, 'id'])}", flush=True)
        pts.loc[lost, "tier"] = "uncertain"
        pts.loc[lost, "radius_m"] = float(cfg.require("road_kansui.snap_m")) * 4
    doc = {"attribution": cfg.require("road_kansui.attribution"),
           "processing": cfg.require("road_kansui.processing_note"),
           "items": RK.display_records(pts)}
    # ★allow_nan=False: NaN が紛れたら書き出しで止める（黙って壊れた JSON を配らない）
    (out_dir / "road_kansui.json").write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
                                               encoding="utf-8")
    from collections import Counter
    print(f"  NK-09 road_kansui.json: {dict(Counter(pts['tier']))}", flush=True)
    return int(len(pts))


# -----------------------------------------------------------------------------
# 3. 全体で決める値（連結旗・最高速度・対象範囲）
# -----------------------------------------------------------------------------
def node_flags(regs: list[Reg], n_nodes: int, min_nodes: int):
    """強連結成分の番号（除外なし・アンダーパス除外）と旗をノードごとに決める。

    ★★成分は**大きい順に番号を振る**（1 = 最大）。北海道・沖縄・離島は本州と道路で
      つながらないので、最大の成分だけに旗を立てると、そこでは経路が一度も引けない
      （2026-09-23 のレビューで指摘）。旗の bit は「番号の付いた成分に入る」。
    """
    ow, up = [], []
    from kansui.connectivity import ONEWAY_TRUE

    for r in regs:
        df = _read(r, ["oneway", "state"])
        ow.append(df["oneway"].astype(str).isin(ONEWAY_TRUE).to_numpy())
        # ★NK-09 の確定の点（known）もアンダーパスなので、除外モードでは除く
        up.append((df["state"] == "underpass").to_numpy() | r.known[r.keep])
    gu = np.concatenate([r.iu for r in regs])
    gv = np.concatenate([r.iv for r in regs])
    ow, up = np.concatenate(ow), np.concatenate(up)
    comp, sizes = C.component_ranks(gu, gv, ow, n_nodes, np.ones(len(gu), dtype=bool), min_nodes)
    comp_x, sizes_x = C.component_ranks(gu, gv, ow, n_nodes, ~up, min_nodes)
    per_node = np.zeros(n_nodes, dtype=np.uint8)
    per_node[comp > 0] |= C.FLAG_MAIN
    per_node[comp_x > 0] |= C.FLAG_MAIN_NO_UNDERPASS
    conn = {
        "nodes": int(n_nodes),
        # 最大の成分（従来の「巨大強連結成分」と同じ意味）
        "main": int(sizes[0]) if len(sizes) else 0,
        "main_share": float(sizes[0] / n_nodes) if len(sizes) else 0.0,
        "main_no_underpass": int(sizes_x[0]) if len(sizes_x) else 0,
        "main_no_underpass_share": float(sizes_x[0] / n_nodes) if len(sizes_x) else 0.0,
        # ★番号の付いた成分（`min_nodes` 以上）の数と、そこに入るノードの割合
        "components": int(len(sizes)),
        "components_share": float((comp > 0).mean()),
        "components_no_underpass": int(len(sizes_x)),
        "components_no_underpass_share": float((comp_x > 0).mean()),
        "min_component_nodes": int(min_nodes),
        "underpass_edges": int(up.sum()),
    }
    return per_node, comp, comp_x, conn


def globals_pass(regs: list[Reg], fix):
    """最高速度（元データ）と対象範囲を、地域ごとに読みながら決める。区画コードもここで付ける。"""
    vmax = 0.0
    cover: dict[str, int] = {}
    for r in regs:
        df = _read(r, ["lon0", "lat0", "lon1", "lat1", "travel_time_s"])
        lon, lat = _coords(r, df, fix)
        # ★★区画コードは**揃えた座標から**計算し直す（揃える前の区画コードのままだと、
        #   ノードが区画の線をまたいで動いたとき持ち主が食い違う。2026-09-23 のレビューで指摘）
        r.mf = MESH.mesh2_codes(lon[:, 0], lat[:, 0]).astype(np.int32)
        r.mt = MESH.mesh2_codes(lon[:, 1], lat[:, 1]).astype(np.int32)
        seg = EM._straight_m(lon, lat)
        raw_t = np.clip(r.tt(df["travel_time_s"].to_numpy()), 0, 6553)
        vmax = max(vmax, EM.max_speed(seg, raw_t))
        for code, bits in MESH.cover3_masks(lon, lat).items():
            cover[code] = cover.get(code, 0) | int(bits, 2)
    cover3 = {k: format(v, "0100b") for k, v in sorted(cover.items())}
    return vmax, cover3


# -----------------------------------------------------------------------------
# 4. 区画の書き出し（1次メッシュのまとまりごと）
# -----------------------------------------------------------------------------
def ensure_elevations(cfg, names: list[str]) -> None:
    """ノードの標高が未作成の地域は、ここで作る（キャッシュ済みの標高タイルだけ。通信しない）。

    ★関東の古い連鎖は標高を付ける段を持っていないので、書き出しの側で補う。
    """
    import subprocess

    for n in names:
        if not (ELEV / f"{n}.parquet").exists():
            print(f"  {n}: ノードの標高を作る（キャッシュのみ）", flush=True)
            # ★失敗しても書き出しは止めない（標高プロファイルが途切れるだけ）
            rc = subprocess.run([sys.executable, "tools/node_elevation.py"],
                                env={**os.environ, "KANSUI_REGION": n}).returncode
            if rc != 0:
                print(f"  ★{n}: ノードの標高を作れなかった（exit={rc}）。標高なしで続ける",
                      flush=True)


def load_elevations(names: list[str]):
    """地域ごとのノード標高（tools/node_elevation.py の出力）を1つの表にする（鍵の昇順）。

    ★無い地域は NaN のまま（標高プロファイルが途切れるだけ。警告を出す）。
    """
    keys, vals, lows = [], [], []
    for n in names:
        p = ELEV / f"{n}.parquet"
        if not p.exists():
            print(f"  ★{n}: ノードの標高が無い（{p}）。KANSUI_REGION={n} uv run python "
                  "tools/node_elevation.py を流すこと", flush=True)
            continue
        d = pd.read_parquet(p)
        keys.append(d["key"].to_numpy(np.float64))
        vals.append(d["elev"].to_numpy(np.float32))
        # ★DEM10B の印（NK-07）。古い出力には無いので False（主系列から取った扱い）で補う
        lows.append(d["low"].to_numpy(bool) if "low" in d.columns else np.zeros(len(d), bool))
    if not keys:
        return np.array([], np.float64), np.array([], np.float32), np.array([], bool)
    k = np.concatenate(keys)
    v = np.concatenate(vals)
    lo = np.concatenate(lows)
    k, first = np.unique(k, return_index=True)
    return k, v[first], lo[first]


def _elev_of(keys: np.ndarray, ek: np.ndarray, ev: np.ndarray, el=None) -> np.ndarray:
    if not len(ek):
        return np.full(keys.shape, np.nan, np.float32)
    at = np.clip(np.searchsorted(ek, keys), 0, len(ek) - 1)
    return np.where(ek[at] == keys, ev[at], np.nan).astype(np.float32)


def _elev_low_of(keys: np.ndarray, ek: np.ndarray, ev: np.ndarray, el: np.ndarray) -> np.ndarray:
    """ノードの標高が DEM10B から取ったものか（NK-07）。無ければ False。"""
    if not len(ek):
        return np.zeros(keys.shape, bool)
    at = np.clip(np.searchsorted(ek, keys), 0, len(ek) - 1)
    return (ek[at] == keys) & el[at]


def export_chunks(regs, per_node, comp, comp_x, fix, v_max_true, out_dir: Path, elev=None):
    from kansui.connectivity import ONEWAY_TRUE

    prefixes = sorted({f"{int(c) // 100:04d}" for r in regs for c in np.unique(
        np.concatenate([r.mf, r.mt]))})
    meshes: dict = {}
    raw = gz = 0
    used: set[str] = set()
    vmax_mps = 0.0
    for pi, pre in enumerate(prefixes, 1):
        c = int(pre)
        parts = []
        for r in regs:
            sel = np.flatnonzero((r.mf // 100 == c) | (r.mt // 100 == c))
            if not len(sel):
                continue
            df = _read(r, COLUMNS).iloc[sel].reset_index(drop=True)
            lon_all, lat_all = _coords_rows(r, df, fix, sel)
            nm = df["name"].astype("string")
            parts.append({
                "key": df[["u", "v"]].to_numpy(np.float64), "lon": lon_all, "lat": lat_all,
                "elev": _elev_of(df[["u", "v"]].to_numpy(np.float64), *elev) if elev else None,
                "elev_low": (_elev_low_of(df[["u", "v"]].to_numpy(np.float64), *elev)
                             if elev else None),
                # ★DEM10B で推定した区間（NK-07）。古い最終表には列が無い → False
                "dem_low": (df["dem_low"].to_numpy(bool) if "dem_low" in df.columns
                            else np.zeros(len(df), bool)),
                "bridge": (r.bridge[r.keep][sel] if r.bridge is not None
                           else np.zeros(len(sel), bool)),
                "flags": np.c_[per_node[r.iu[sel]], per_node[r.iv[sel]]].astype(np.uint8),
                "comp": np.c_[comp[r.iu[sel]], comp[r.iv[sel]]],
                "comp_x": np.c_[comp_x[r.iu[sel]], comp_x[r.iv[sel]]],
                "mf": r.mf[sel].astype(np.int64), "mt": r.mt[sel].astype(np.int64),
                # ★型は最終表のまま（float32）。上げると丸めの境目で 0.1秒・1段ずれる（回帰で実測）
                "tt": r.tt(df["travel_time_s"].to_numpy(), sel),
                "oneway": df["oneway"].astype(str).isin(ONEWAY_TRUE).to_numpy(),
                "label": nm.where(nm.notna() & (nm != ""), df["ref"].astype("string")),
                # ★NK-09: 確定の点に結び付いた区間は最上位のスコア（藤沢版 F_known のハード上書きと同じ）
                "s_topo": np.where(r.known[r.keep][sel], 1.0, df["s_topo"].to_numpy()),
                "state": np.select([r.known[r.keep][sel], (df["state"] == "underpass").to_numpy()],
                                   [EM.STATE_CODE["known"], EM.STATE_CODE["underpass"]],
                                   EM.STATE_CODE["topo"]),
                "narrow": df["narrow"].to_numpy(),
            })
            del df
        cat = pd.concat([p["label"] for p in parts], ignore_index=True)
        cat = cat.replace("", pd.NA).astype("category")
        def cc(k, _parts=parts):
            return np.concatenate([p[k] for p in _parts])
        res = EM.export(
            {"travel_time_s": cc("tt"), "oneway": cc("oneway"),
             "name_codes": cat.cat.codes.to_numpy(),
             "name_cats": [str(x) for x in cat.cat.categories]},
            {"s_topo": cc("s_topo"), "state_code": cc("state").astype(np.uint8)},
            cc("mf"), cc("mt"), np.concatenate([p["key"] for p in parts]),
            np.concatenate([p["lon"] for p in parts]), np.concatenate([p["lat"] for p in parts]),
            np.concatenate([p["flags"] for p in parts]), cc("narrow"),
            out_dir=out_dir, region_name="", v_max_true=v_max_true, only_prefix=pre,
            node_comp=np.concatenate([p["comp"] for p in parts]),
            node_comp_x=np.concatenate([p["comp_x"] for p in parts]),
            node_elev=(np.concatenate([p["elev"] for p in parts]) if elev else None),
            node_elev_low=(np.concatenate([p["elev_low"] for p in parts]) if elev else None),
            edge_bridge=np.concatenate([p["bridge"] for p in parts]),
            edge_dem_low=np.concatenate([p["dem_low"] for p in parts]),
            write_index=False,
        )
        del parts, cat
        meshes.update(res["meshes_info"])
        raw += res["raw"]
        gz += res["gzip"]
        used |= res["used_names"]
        vmax_mps = max(vmax_mps, res["vmax_mps"])
        print(f"  [{pi}/{len(prefixes)}] 1次区画 {pre}: {len(res['meshes_info'])} 区画", flush=True)
    return {"meshes": dict(sorted(meshes.items())), "raw": raw, "gzip": gz,
            "names": len(used), "vmax_mps": vmax_mps}


def _coords_rows(r: Reg, df: pd.DataFrame, fix, sel):
    """`df` は `sel` の行だけ。揃えた座標を返す。"""
    # ★★必ず複製する。pandas 3 はコピーオンライトが既定で、float64 の列の to_numpy は
    #   **書き込めないビュー**を返す。補正を書き込むところで ValueError になり、PBF の日付が違う
    #   地域を混ぜた書き出しが必ず落ちていた（2026-09-23 のレビューで指摘。同じ PBF では通らない）
    lon = np.array(df[["lon0", "lon1"]].to_numpy(np.float64), copy=True)
    lat = np.array(df[["lat0", "lat1"]].to_numpy(np.float64), copy=True)
    fn, fx, fy = fix
    if len(fn):
        for side, ids in enumerate((r.iu[sel], r.iv[sel])):
            at = np.searchsorted(fn, ids)
            hit = (at < len(fn)) & (fn[np.minimum(at, len(fn) - 1)] == ids)
            lon[hit, side] = fx[at[hit]]
            lat[hit, side] = fy[at[hit]]
    return lon, lat


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("kansui.export_mesh").setLevel(logging.WARNING)
    cfg = Config.load()
    # ★作成を流しながら順に広げるときは環境変数で上書きする（config を書き換えずに済む）
    env = os.environ.get("KANSUI_NATIONAL_REGIONS", "").strip()
    names = [x.strip() for x in env.split(",") if x.strip()] if env else \
        [str(x) for x in cfg.require("national.regions")]
    regions = cfg.require("regions")
    unknown = [n for n in names if n not in regions]
    if unknown:
        raise SystemExit(f"★regions に無い地域: {unknown}")
    # ★★いまの配布データに入っている地域を**黙って落とさない**（2026-09-24、手で書き出し直したとき
    #   茨城を入れ忘れて配布から外してしまった。全国の連鎖は配布済みの地域を出発点にするので、
    #   気づかなければ茨城を一から作り直すところだった）。本当に外すときは KANSUI_ALLOW_DROP=1
    cur = OUT / "index.json"
    if cur.exists() and os.environ.get("KANSUI_ALLOW_DROP") != "1":
        import json

        had = json.loads(cur.read_text(encoding="utf-8")).get("regions") or []
        lost = [n for n in had if n not in names]
        if lost:
            raise SystemExit(f"★いまの配布データにある地域が外れる: {lost}。"
                             "外すなら KANSUI_ALLOW_DROP=1 を付けること")
    # ★画面に出す名前は各地域の label をつなぐ（手で書くと足し忘れる）
    label = "・".join(str(regions[n].get("label") or n) for n in names)
    print(f"地域: {', '.join(names)}", flush=True)
    from kansui import roads as R

    pbf_of_rank = [str(R.pbf_path_for_region(cfg, n)) for n in names]

    regs, info = select_rows(names, cfg)
    #   ★PBF の日付が違う地域どうし（関東 2026-08-25 と他地方 2026-09-22）では、
    #     その間に OSM でノードが動かされていれば食い違う。
    #     → **優先する側（始点が内側 → 地域の順）の座標に揃え**、件数とずれの最大を出す。
    fix = harmonize(regs, info["nodes"], pbf_of_rank)
    per_node, comp, comp_x, conn = node_flags(
        regs, info["nodes"], int(cfg.require("connectivity.min_component_nodes")))
    print(f"巨大強連結成分: 除外なし {conn['main']:,}/{conn['nodes']:,} "
          f"({conn['main_share']:.2%}) / アンダーパス除外 "
          f"{conn['main_no_underpass']:,} ({conn['main_no_underpass_share']:.2%})", flush=True)
    print(f"番号を振った成分（{conn['min_component_nodes']} ノード以上）: 除外なし "
          f"{conn['components']:,} 個・{conn['components_share']:.2%} / アンダーパス除外 "
          f"{conn['components_no_underpass']:,} 個・{conn['components_no_underpass_share']:.2%}",
          flush=True)
    v_max_true, cover3 = globals_pass(regs, fix)
    if not np.isfinite(v_max_true) or v_max_true <= 0:
        raise SystemExit(f"★元データの最高速度が求まらない: {v_max_true}")

    # ★★一時置き場に書き出してから入れ替える。区画のループの途中で落ちると、
    #   古い index.json と半分だけ新しい区画ファイルが混ざったまま配られる
    #   （最高速度・区画の対応が食い違う。2026-09-23 のレビューで指摘）
    tmp = OUT.with_name(OUT.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "mesh").mkdir(parents=True)
    res = export_chunks(regs, per_node, comp, comp_x, fix, v_max_true, tmp,
                        elev=(ensure_elevations(cfg, names), load_elevations(names))[1])
    print(f"最高速度: 元データ {v_max_true * 3.6:.2f} km/h → 量子化後 "
          f"{res['vmax_mps'] * 3.6:.2f} km/h（★これを配る）", flush=True)
    EM.write_index_json(tmp, res["meshes"], res["vmax_mps"], "+".join(names), {
        "lambdaDefault": float(cfg.require("routing.lambda_default")),
        "lambdaSteps": [float(x) for x in cfg.require("routing.lambda_steps")],
        "muNarrowDefault": float(cfg.require("routing.mu_narrow_default")),
        "calibratedDepthM": float(cfg.require("routing.calibrated_depth_m")),
        "calibratedThreshold": float(cfg.require("routing.calibrated_threshold")),
        "acceptableTimeRatio": float(cfg.require("routing.acceptable_time_ratio")),
        "hazardList": {
            "maxItems": int(cfg.require("hazard_list.max_items")),
            "mergeGapM": float(cfg.require("hazard_list.merge_gap_m")),
        },
        "maxRouteDistanceKm": float(cfg.require("routing.max_route_distance_km")),
        # ★40km を超える経路の上限（NK-10）。読む量（区画の gzip の合計）で決める
        "longRoute": {
            "maxKm": float(cfg.require("routing.long_route.max_km")),
            "maxLoadMB": float(cfg.require("routing.long_route.max_load_mb")),
            "marginKm": float(cfg.require("routing.long_route.margin_km")),
        },
        "useAstar": bool(cfg.require("routing.use_astar")),
        # ★NK-09: 道路冠水想定箇所の件数（0 なら画面の出典・免責を出さない）
        "roadKansuiCount": write_road_kansui(cfg, tmp, names),
        "meshLevel": int(cfg.require("split.level")),
        # ★範囲の外をタップしたときの説明に使う（→ router.worker.ts）
        "regionLabel": label,
        "regions": names,
        # ★★対象範囲の形は**道路（OSM）から**作る。行政区域（N03）の形は配らない
        #   （N03 は測量法の複製承認の上に成り立ち、二次利用に申請が要る場合がある）。
        "cover3": cover3,
        "coverClosing": int(cfg.require("coverage.closing_cells")),
        # ★幅員は国土地理院の幅員区分（NK-18）。照合できなかったぶんだけ道路種別の近似
        "narrowSource": "gsi_rnkwidth",
        # ★連結性の実測（誇張しない。UI の文言の根拠にする）
        "connectivity": conn,
    })
    meshes = res["meshes"]
    print(f"\n★区画 {len(meshes)} 枚 / 道路名 {res['names']:,} 種", flush=True)
    print(f"  そのまま {res['raw'] / 1e6:.1f}MB / gzip {res['gzip'] / 1e6:.1f}MB", flush=True)
    print(f"  最大の区画 {max(m['gzip'] for m in meshes.values()) / 1e6:.2f}MB (gz)", flush=True)
    limit = int(cfg.require("split.max_file_bytes"))
    big = max(m["bytes"] for m in meshes.values())
    if big > limit:
        shutil.rmtree(tmp)
        raise SystemExit(f"★1ファイル上限 {limit:,} を超える区画がある: {big:,}"
                         "（配布データは前のまま）")
    # ★★この書き出しが作らないファイル（住所検索の places.json など）は引き継ぐ。
    #   置き場を丸ごと入れ替えるので、引き継がないと**黙って消える**
    #   （2026-09-29、住所検索が「検索データを読み込めませんでした」になって判明）。
    #   ★作るもの（index.json・mesh/）は引き継がない（消えた区画を残さないため）
    if OUT.exists():
        for f in OUT.iterdir():
            # ★road_kansui.json も作るもの（点が0件なら作らない＝古いものを残してはいけない）
            if f.name in ("index.json", "mesh", "road_kansui.json") or (tmp / f.name).exists():
                continue
            # ★書きかけ・入れ替えの残り（*.part・*.tmp・*.old）は引き継がない（7回目のレビュー）
            if f.name.endswith((".part", ".tmp", ".old")) or f.is_symlink():
                continue
            (shutil.copytree if f.is_dir() else shutil.copy2)(f, tmp / f.name)
            print(f"  引き継いだ: {f.name}", flush=True)
        # ★住所検索は対象範囲（cover3）で絞っている。範囲が変わったら作り直しが要る（黙って古いまま配らない）
        pl = tmp / "places.json"
        if pl.exists():
            import hashlib
            import json

            want = hashlib.sha1(json.dumps(cover3, sort_keys=True).encode()).hexdigest()
            got = json.loads(pl.read_text(encoding="utf-8")).get("cover3Hash")
            if got != want:
                print("  ★places.json は今と違う対象範囲で作られている。"
                      "`uv run python tools/build_places.py` で作り直すこと", flush=True)
    # ★入れ替え。消えた区画のファイルも一緒に消える（新しい置き場には今回の区画しか無い）
    old = OUT.with_name(OUT.name + ".old")
    if old.exists():
        shutil.rmtree(old)
    if OUT.exists():
        OUT.rename(old)
    tmp.rename(OUT)
    if old.exists():
        shutil.rmtree(old)
    print(f"  → {OUT}（入れ替えた）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
