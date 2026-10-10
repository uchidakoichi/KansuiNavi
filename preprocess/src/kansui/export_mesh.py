"""区画（2次地域区画）ごとの配布データを書き出す。

★全国版の核心。藤沢版は `graph.bin` **1本**で市域+3km 全部を配っていたが、
  全国版は**区画ごとに分け、経路探索が触った区画だけを読む**（F-11 / NK-06 決定）。

━━ 形式（`mesh/{code}.bin`）━━━━━━━━━━━━━━━━━━━━━━━━━━━
  nodeLon    uint32  n   固定小数。★原点は**全国固定**
  nodeLat    uint32  n
  nodeFlags  uint8   n   bit0 = 番号付きの強連結成分に入る（除外なし）/ bit1 = 同（除外あり）
  nodeComp   uint16  n   強連結成分の番号（1 = 最大。0 = 小さい断片）… 除外なし   ★形式 5
  nodeCompX  uint16  n   同 … アンダーパス除外
  nodeElev   int16   n   標高［0.1m］。不明は -32768（0m と混同しない。T-09）   ★形式 6（F-03）
  edgeBridge uint8   m   橋（1）・トンネル（2）の区間。プロファイルで両端を結ぶ   ★形式 7（F-03）。2 は 2026-09-29
  edgeDemLow uint8   m   DEM10B（精度の低い標高）で推定した区間（1）   ★形式 8（NK-07）
  nodeElevLow uint8  n   ノードの標高が DEM10B から（1）。プロファイルで破線   ★形式 8（NK-07）
                          ★**起終点をここに寄せる**。→ `kansui.connectivity`
  edgeU      uint32  m   ★**区画内のローカル索引**
  edgeV      uint32  m
  edgeTime   uint16  m   0.1秒単位
  edgeScore  uint8   m   S_topo × 254。★**255 = 判定不能**（0 と混同しない）
  edgeState  uint8   m   0=topo / 1=underpass / 2=known
  edgeOneway uint8   m
  edgeNarrow uint8   m   narrowness × 254
  edgeName   uint16  m   地域の名前テーブルの索引。0 = 無し
  bKey       float64 b   ★**この区画のノードのうち、他の区画からも参照されるもの**の鍵
  bLocal     uint32  b   そのノードのローカル索引
  fKey       float64 f   ★**他の区画に属するノード**（またぐエッジの相手側）の鍵
  fLocal     uint32  f   そのノードのローカル索引
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

★★**2つに分ける理由**（2026-09-21 にバグを踏んで分かった）:
  はじめは「他の区画に属するノード」だけを記録していた。すると
  **区画 A は「B のノード b」の鍵を書くが、区画 B は自分の b の鍵を書かない。**
  → クライアントが A→B の順に読むと b が**二重に作られ**、
    A 側のエッジと B 側のエッジが**つながらない。**
  実測: 統合後の最大連結成分が **35.76%**（元のグラフは 99.20%）。
  ★例外は出ない。**経路が区画の縁で切れるだけ。**
  → 検証は `tools/verify_mesh_merge.py`。**この検査を外さないこと。**

★**3つの設計判断**（いずれも「地域を足して積み上げる」ためのもの）:

1. **座標の原点を全国で固定する。**
   地域ごとの原点にすると、同じ区画でも作ったビルドによって中身が変わる。
   日本の範囲（東経122〜154度・北緯20〜46度）は 1e-7 度刻みでも uint32 に収まる。

2. **ノード索引は区画内のローカルにする。**
   地域全体の通し番号にすると、区画ファイルが「どの地域のビルドか」に依存する。

3. ★**境界ノードは OSM node ID を鍵にして同一視する。**
   2つの区画が同じノードを指していることを、クライアントが確実に判定できる。
   **座標の一致で判定してはいけない**（固定小数の丸めで崩れる）。
   OSM node ID は 2^53 未満なので float64 で正確に運べる。

★**境界をまたぐエッジは両方の区画に入れる。** 実測で 0.32〜0.35% しかなく、
  合計サイズにほとんど出ない。入れないと**経路が区画の縁で切れる**。
"""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:  # pragma: no cover
    import geopandas as gpd
    import pandas as pd

log = logging.getLogger(__name__)

#: ★座標の原点。**全国固定**（日本の南西の外側）。地域ごとに変えない。
ORIGIN_LON, ORIGIN_LAT = 122.0, 20.0
#: 1e-7 度 ≒ 1cm。日本の範囲でも uint32（最大 4.29e9）に収まる
COORD_SCALE = 1e7
TIME_SCALE = 10           # 0.1秒単位
SCORE_SCALE = 254         # S_topo 0〜1 → 0〜254
SCORE_UNKNOWN = 255       # ★判定不能。0（＝最も安全）と混同しない
NARROW_SCALE = 254
STATE_CODE = {"topo": 0, "underpass": 1, "known": 2}

#: ★2 で `nodeFlags`、3 で `vmaxMps` を足した。**古い index.json と混ぜない**ために上げる
#: ★4 で**道路名の表と配置（layout）を区画ファイルの先頭へ移した**（2026-09-23）。
#:   全国では index.json が約9MB（配置 約4MB＋道路名 約4MB）になり、起動のたびに読むには重い。
#:   さらに道路名は全国で 65,535 種を超え、Uint16 の索引に収まらない。
#:   → 区画ごとの表にすれば1区画の中で数千種に収まる。
FORMAT_VERSION = 8
#: ★8 で **DEM10B の印**（edgeDemLow / nodeElevLow）を足した（2026-09-28・NK-07）。
#:   DEM5A〜5C の無い区間を DEM10B で埋めるようにしたので、精度の違いを画面で出し分ける（T-11）。
#: ★5 で**強連結成分の番号**（nodeComp / nodeCompX）を足した（2026-09-23）。
#:   北海道・沖縄・離島は本州と道路でつながらないので、「最大の成分」1つでは足りない。
#: ★6 で**ノードの標高**（nodeElev）を足した（2026-09-24。F-03 ルート標高プロファイル）。
#:   国土地理院から「標高タイルは申請不要・出典の明示で利用可」と回答があったため。
ELEV_SCALE = 10
ELEV_UNKNOWN = -32768
#: 区画ファイルの先頭4バイト。★形式の取り違えを中身で見分ける
MESH_MAGIC = b"KNM4"

#: ★A* のヒューリスティックが使う平面近似の係数。
#:   **`web/src/router.worker.ts` の `h()` と1桁も違えてはいけない。**
#:   ここで出す上限速度は、向こうと同じ距離の測り方でなければ保証にならない。
M_PER_DEG_LON = 111320.0
M_PER_DEG_LAT = 110540.0


class ExportError(RuntimeError):
    """書き出せない。★黙って壊れたファイルを作らない。"""


def write_index_json(out_dir: Path, meshes: dict, vmax_mps: float, region_name: str,
                extra_meta: dict | None = None) -> dict:
    """index.json を書く（区画の一覧・量子化の尺度・最高速度・その他のメタ情報）。"""
    index = {
        "format": FORMAT_VERSION,
        "region": region_name,
        "origin": [ORIGIN_LON, ORIGIN_LAT],
        "coordScale": COORD_SCALE,
        "timeScale": TIME_SCALE,
        "scoreScale": SCORE_SCALE,
        "scoreUnknown": SCORE_UNKNOWN,
        "narrowScale": NARROW_SCALE,
        "elevScale": ELEV_SCALE,
        "elevUnknown": ELEV_UNKNOWN,
        # ★A* のヒューリスティックの分母。**配布データから出す**（コードに直書きしない）。
        #   これを下回る値を使うと見積もりが過大になり、最短経路である保証が崩れる。
        "vmaxMps": vmax_mps,
        "state": STATE_CODE,
        "meshes": meshes,
        **(extra_meta or {}),
    }
    (Path(out_dir) / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    return index


def max_speed(seg: np.ndarray, raw_t: np.ndarray) -> float:
    """元データの最高速度［m/s］＝ 直線距離 ÷ 通過時間 の最大（通過時間が 0 の辺は除く）。"""
    with np.errstate(divide="ignore", invalid="ignore"):
        v_true = np.where(raw_t > 0, seg / raw_t, 0.0)
    return float(np.nanmax(v_true)) if len(v_true) else 0.0


def _straight_m(node_lon: np.ndarray, node_lat: np.ndarray) -> np.ndarray:
    """エッジの両端の**直線距離**［m］。★`router.worker.ts` の `h()` と同じ測り方。"""
    lat0, lat1 = node_lat[:, 0], node_lat[:, 1]
    k = np.cos(np.radians((lat0 + lat1) / 2.0))
    dx = (node_lon[:, 1] - node_lon[:, 0]) * k * M_PER_DEG_LON
    dy = (lat1 - lat0) * M_PER_DEG_LAT
    return np.hypot(dx, dy)


def _fixed(deg: np.ndarray, origin: float) -> np.ndarray:
    v = np.rint((deg - origin) * COORD_SCALE)
    if v.min() < 0 or v.max() > np.iinfo(np.uint32).max:
        raise ExportError(
            f"固定小数が uint32 に収まらない: {v.min()}〜{v.max()}。原点を確認すること"
        )
    return v.astype(np.uint32)


def build_name_table(labels) -> tuple[np.ndarray, list[str]]:
    """道路名 → Uint16 の索引。0 は「名前なし」。

    ★藤沢版と同じ考え方。**上限を超えたら黙って壊れる前に落とす。**
    """
    import pandas as pd

    s = pd.Series(labels).astype("string")
    uniq = [x for x in s.dropna().unique().tolist() if x]
    if len(uniq) + 1 > 65535:
        raise ExportError(f"道路名が Uint16 に収まらない: {len(uniq)} 種")
    idx = {name: i + 1 for i, name in enumerate(uniq)}
    return s.map(lambda v: idx.get(v, 0)).fillna(0).to_numpy().astype(np.uint16), uniq


def _with_header(blob: bytes, layout: dict, names: list[str]) -> bytes:
    """区画ファイル = MAGIC(4) + ヘッダ長(uint32 LE) + ヘッダ JSON + 8バイト境界まで詰め物 + 本体。

    ★本体の中の offset は**本体の先頭から**数える（ヘッダの長さに依存させない）。
    """
    head = json.dumps({"layout": layout, "names": names}, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    out = bytearray(MESH_MAGIC + len(head).to_bytes(4, "little") + head)
    while len(out) % 8:
        out += b"\0"
    return bytes(out) + blob


def read_mesh(buf: bytes) -> tuple[dict[str, np.ndarray], list[str]]:
    """区画ファイルを配列と道路名の表に戻す（検算用。★クライアントの読み方と同じ）。"""
    if buf[:4] != MESH_MAGIC:
        raise ExportError(f"区画ファイルの形式が違う: {buf[:4]!r}")
    n = int.from_bytes(buf[4:8], "little")
    head = json.loads(buf[8:8 + n].decode("utf-8"))
    start = 8 + n
    start += (-start) % 8
    arrays = {k: np.frombuffer(buf, dtype=np.dtype(v["type"]), count=v["length"],
                               offset=start + v["offset"])
              for k, v in head["layout"].items()}
    return arrays, head["names"]


def _pack(arrays: list[tuple[str, np.ndarray]]) -> tuple[bytes, dict]:
    blob = bytearray()
    layout = {}
    for name, arr in arrays:
        layout[name] = {"offset": len(blob), "length": int(arr.size), "type": arr.dtype.name}
        blob += arr.tobytes()
        while len(blob) % 8:        # ★float64 のアラインメントまで揃える
            blob += b"\0"
    return bytes(blob), layout


def export(
    edges: gpd.GeoDataFrame,
    risk: pd.DataFrame,
    mesh_from: np.ndarray,
    mesh_to: np.ndarray,
    node_key: np.ndarray,
    node_lon: np.ndarray,
    node_lat: np.ndarray,
    node_flags: np.ndarray,
    narrowness: np.ndarray,
    out_dir: Path,
    region_name: str,
    extra_meta: dict | None = None,
    v_max_true: float | None = None,
    node_comp: np.ndarray | None = None,
    node_comp_x: np.ndarray | None = None,
    node_elev: np.ndarray | None = None,
    edge_bridge: np.ndarray | None = None,
    node_elev_low: np.ndarray | None = None,
    edge_dem_low: np.ndarray | None = None,
    only_prefix: str | None = None,
    write_index: bool = True,
) -> dict:
    """区画ごとの `.bin` と `index.json` を書き出す。

    `edges` の各行に対し、`mesh_from` / `mesh_to` は**両端ノードの区画コード**。
    `node_key` / `node_lon` / `node_lat` は**エッジの行と同じ長さ × 2**ではなく、
    エッジの u / v に対応する2列の配列（shape (len(edges), 2)）。

    ★全国では1次メッシュのまとまりごとに呼ぶ（tools/export_national.py）:
      v_max_true  … 通過時間の量子化に使う最高速度［m/s］。**全体で1つ**の値を外から渡す
      only_prefix … この文字列で始まる区画だけ書く（またぐエッジの相手側の区画は書かない）
      write_index … False なら index.json を書かない（最後にまとめて書く）
    """
    import pandas as pd

    out_dir = Path(out_dir)
    (out_dir / "mesh").mkdir(parents=True, exist_ok=True)

    # ★全国では pandas の表を作ると複製でメモリが倍々になるので、**配列の辞書**でも受け取る:
    #   edges = {"travel_time_s", "oneway"(bool), "name_codes"(int, -1=なし), "name_cats"(list)}
    #   risk  = {"s_topo"(float), "state_code"(uint8)}
    arrays_in = isinstance(edges, dict)
    n_edges = len(edges["travel_time_s"]) if arrays_in else len(edges)
    for name, arr in (("mesh_from", mesh_from), ("mesh_to", mesh_to),
                      ("narrowness", narrowness)):
        if len(arr) != n_edges:
            raise ExportError(f"{name} の長さがエッジ数と違う: {len(arr)} vs {n_edges}")
    for name, arr2 in (("node_key", node_key), ("node_lon", node_lon),
                       ("node_lat", node_lat), ("node_flags", node_flags)):
        if arr2.shape != (n_edges, 2):
            raise ExportError(f"{name} の形が (エッジ数, 2) でない: {arr2.shape}")

    if arrays_in:
        s = np.asarray(risk["s_topo"])
    else:
        r = risk.reindex(edges.index)
        s = r["s_topo"].to_numpy()
    # ★S_topo が NaN ＝ 判定不能。255 の番兵に落とす。0 にしない
    score = np.where(np.isnan(s), SCORE_UNKNOWN,
                     np.rint(np.clip(s, 0, 1) * SCORE_SCALE)).astype(np.uint8)
    state = (np.asarray(risk["state_code"], dtype=np.uint8) if arrays_in
             else r["state"].map(STATE_CODE).fillna(0).to_numpy().astype(np.uint8))
    # --- 通過時間の量子化（★A* の許容性に直結する） ----------------------------
    # ★★2026-09-21 に2つの誤りが見つかった:
    #   (1) `travel_time_s` が小さいエッジは 0.1秒に丸めると **0 になる**（1,351本）。
    #       費用 0 のリンク＝**ただで通れる道**になってしまう。
    #   (2) 丸めで短くなると「直線距離 ÷ 時間」が跳ね上がり、量子化後の最高速度が
    #       **149.0km/h** になっていた（元データは 104.5km/h）。
    #       A* のヒューリスティックは `直線距離 ÷ 最高速度` なので、
    #       **見積もりが過大になり最短経路である保証が崩れる。**
    # → 元データの最高速度を超えないところまで**切り上げる**。
    #   実測の代償は 45km の経路で **約 0.27秒**（該当は全エッジの 0.149%）。
    seg = _straight_m(node_lon, node_lat)
    raw_t = np.clip(np.asarray(edges["travel_time_s"]), 0, 6553)
    if v_max_true is None:
        v_max_true = max_speed(seg, raw_t)
    if not np.isfinite(v_max_true) or v_max_true <= 0:
        raise ExportError(f"元データの最高速度が求まらない: {v_max_true}")
    need = np.ceil(seg / v_max_true * TIME_SCALE)
    time = np.maximum(np.rint(raw_t * TIME_SCALE), need)
    # ★★長さ 0 の区間（OSM で**別のノードが同じ座標に重なっている**）は、元の通過時間も 0 で
    #   上の2つがどちらも 0 になる（千葉で1本。2026-09-24 に書き出しが止まった）。
    #   → 最小 1 単位（0.1秒）にする。長さ 0 なので A* の見積もりの差も 0 で、許容性は崩れない。
    #   ★0 のまま配ると「ただで通れる道」になる（2026-09-21 の (1) と同じ）。
    time = np.maximum(time, 1)
    if (time <= 0).any():
        raise ExportError(f"通過時間が 0 のエッジが {int((time <= 0).sum())} 本ある")
    if time.max() > np.iinfo(np.uint16).max:
        raise ExportError(f"通過時間が uint16 に収まらない: {time.max()}")
    time = time.astype(np.uint16)
    # ★配るのは**量子化後の**上限。クライアントが実際に使う値から出す
    vmax_mps = float((seg / (time.astype(np.float64) / TIME_SCALE)).max())
    log.info("最高速度: 元データ %.2f km/h → 量子化後 %.2f km/h（★これを配る）",
             v_max_true * 3.6, vmax_mps * 3.6)
    ow = edges["oneway"]
    # ★bool で渡されたらそのまま（全国で数千万本を文字列に直さない）
    oneway = (np.asarray(ow) if (arrays_in or ow.dtype == bool)
              else ow.astype(str).isin(["yes", "true", "1", "-1"]).to_numpy())
    oneway = oneway.astype(np.uint8)
    narrow = np.rint(np.clip(narrowness, 0, 1) * NARROW_SCALE).astype(np.uint8)

    # ★道路名は**カテゴリの番号**で持つ（全国で数千万本の文字列を作らない）。
    #   `name` がカテゴリ型ならそのまま、そうでなければ `ref` で補ってカテゴリにする。
    if arrays_in:
        name_codes = np.asarray(edges["name_codes"])
        name_cats = list(edges["name_cats"])
    else:
        label = edges["name"] if "name" in edges.columns else pd.Series([None] * n_edges)
        if not isinstance(label.dtype, pd.CategoricalDtype):
            if "ref" in edges.columns:
                label = label.fillna(edges["ref"])
            label = label.astype("string").replace("", pd.NA).astype("category")
        name_codes = label.cat.codes.to_numpy()            # -1 = 名前なし
        name_cats = [str(c) for c in label.cat.categories]
    used_names = np.zeros(len(name_cats), dtype=bool)

    # --- エッジを区画へ割り当てる（★またぐものは両方に） -------------------------
    #   ★Python のリストで持たない（全国で数千万要素）。区画コード → 行の番号を並べ替えで作る。
    #   各区画の中の行は**エッジの番号の昇順**（従来の並びと同じ）。
    mesh_from = np.asarray(mesh_from).astype(np.int64)
    mesh_to = np.asarray(mesh_to).astype(np.int64)
    cross = mesh_from != mesh_to
    all_idx = np.arange(n_edges, dtype=np.int64)
    gkey = np.concatenate([mesh_from, mesh_to[cross]])
    gidx = np.concatenate([all_idx, all_idx[cross]])
    order = np.lexsort((gidx, gkey))
    gkey, gidx = gkey[order], gidx[order]
    cut = np.flatnonzero(np.diff(gkey)) + 1
    by_mesh = {str(int(k)): r for k, r in zip(gkey[np.r_[0, cut]], np.split(gidx, cut),
                                                strict=True)} if n_edges else {}

    meshes: dict[str, dict] = {}
    total_raw = total_gz = 0
    for code in sorted(by_mesh):
        if only_prefix is not None and not code.startswith(only_prefix):
            continue
        rows = by_mesh[code]
        keys = node_key[rows].ravel()           # (2*len(rows),)
        lons = node_lon[rows].ravel()
        lats = node_lat[rows].ravel()
        flags = node_flags[rows].ravel()
        uniq, inverse = np.unique(keys, return_inverse=True)
        first = np.zeros(len(uniq), dtype=np.int64)
        first[inverse[::-1]] = np.arange(len(inverse))[::-1]   # 各ノードの最初の出現
        nlon = _fixed(lons[first], ORIGIN_LON)
        nlat = _fixed(lats[first], ORIGIN_LAT)
        nflag = flags[first].astype(np.uint8)
        comp_arrays = []
        if node_comp is not None:
            comp_arrays = [("nodeComp", node_comp[rows].ravel()[first].astype(np.uint16)),
                           ("nodeCompX", node_comp_x[rows].ravel()[first].astype(np.uint16))]
        if node_elev is not None:
            ne = node_elev[rows].ravel()[first]
            comp_arrays.append(("nodeElev", np.where(
                np.isnan(ne), ELEV_UNKNOWN,
                np.rint(np.clip(ne, -3276, 3276) * ELEV_SCALE)).astype(np.int16)))
            if node_elev_low is not None:
                comp_arrays.append(("nodeElevLow", np.asarray(node_elev_low)[rows]
                                    .ravel()[first].astype(np.uint8)))
        eu = inverse.reshape(-1, 2)[:, 0].astype(np.uint32)
        ev = inverse.reshape(-1, 2)[:, 1].astype(np.uint32)

        # ★★またぐエッジの**両端**を記録する。片側だけだと二重に作られてつながらない。
        #   bset … この区画が持ち主で、他の区画からも参照されるノード
        #   fset … 他の区画が持ち主のノード（＝この区画では「縁」）
        code_i = int(code)
        bset: dict[float, int] = {}
        fset: dict[float, int] = {}
        # ★またいでいるエッジだけを見る（Python のループを全エッジに回さない）
        for k in np.flatnonzero(cross[rows]).tolist():
            i = int(rows[k])
            for side, m in ((0, mesh_from[i]), (1, mesh_to[i])):
                key = float(node_key[i, side])
                local = int(inverse[2 * k + side])
                (bset if m == code_i else fset)[key] = local
        bkey = np.array(sorted(bset), dtype=np.float64)
        blocal = np.array([bset[k] for k in bkey], dtype=np.uint32)
        fkey = np.array(sorted(fset), dtype=np.float64)
        flocal = np.array([fset[k] for k in fkey], dtype=np.uint32)

        # ★道路名は**区画ごとの表**（→ FORMAT_VERSION 4 の注記）
        # ★区画の表は「その区画に出てくる順」（従来の build_name_table と同じ並び）
        sub = name_codes[rows]
        has = sub >= 0
        uq, first = np.unique(sub[has], return_index=True)
        uq = uq[np.argsort(first)]
        if len(uq) + 1 > 65535:
            raise ExportError(f"区画 {code} の道路名が Uint16 に収まらない: {len(uq)} 種")
        local = np.zeros(len(name_cats) + 1, dtype=np.uint16)
        local[uq + 1] = np.arange(1, len(uq) + 1, dtype=np.uint16)
        name_idx = local[sub + 1]
        name_table = [name_cats[c] for c in uq]
        used_names[uq] = True
        blob, layout = _pack([
            ("nodeLon", nlon), ("nodeLat", nlat), ("nodeFlags", nflag), *comp_arrays,
            ("edgeU", eu), ("edgeV", ev),
            ("edgeTime", time[rows]), ("edgeScore", score[rows]),
            ("edgeState", state[rows]), ("edgeOneway", oneway[rows]),
            ("edgeNarrow", narrow[rows]), ("edgeName", name_idx),
            *([("edgeBridge", np.asarray(edge_bridge)[rows].astype(np.uint8))]
              if edge_bridge is not None else []),
            *([("edgeDemLow", np.asarray(edge_dem_low)[rows].astype(np.uint8))]
              if edge_dem_low is not None else []),
            ("bKey", bkey), ("bLocal", blocal),
            ("fKey", fkey), ("fLocal", flocal),
        ])
        blob = _with_header(blob, layout, name_table)
        gz = gzip.compress(blob, 9, mtime=0)   # ★mtime=0: 同じ中身なら同じ .gz（差分を追える）
        # ★展開済みの `.bin` は書かない。クライアントは `.bin.gz` しか取りに行かない
        #   （配信側が先に展開する場合も T-14 のとおり中身で見分ける）。
        #   書くと配信ファイル数とディスクが倍になる（全国で約4,500区画 → 9,000ファイル）。
        (out_dir / "mesh" / f"{code}.bin.gz").write_bytes(gz)
        meshes[code] = {"nodes": int(len(uniq)), "edges": int(len(rows)),
                        "boundary": int(len(bkey)), "foreign": int(len(fkey)),
                        "bytes": len(blob),
                        "gzip": len(gz)}
        total_raw += len(blob)
        total_gz += len(gz)
        log.info("  %s  ノード %6d / エッジ %6d / 共有 %4d / 縁 %4d / %6.2fMB (gz %5.2fMB)",
                 code, len(uniq), len(rows), len(bkey), len(fkey),
                 len(blob) / 1e6, len(gz) / 1e6)

    used = {name_cats[i] for i in np.flatnonzero(used_names)}
    if not write_index:
        return {"meshes_info": meshes, "raw": total_raw, "gzip": total_gz,
                "used_names": used, "vmax_mps": vmax_mps}
    index = write_index_json(out_dir, meshes, vmax_mps, region_name, extra_meta)
    return {"meshes": len(meshes), "raw": total_raw, "gzip": total_gz,
            "names": int(used_names.sum()), "index": index}
