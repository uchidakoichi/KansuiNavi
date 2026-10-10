"""国土地理院 最適化ベクトルタイル（`optimal_bvmap`）の取得と復号。

★全国の道路に**幅員区分**（`vt_rnkwidth`）とアンダーパスの手がかり
  （`vt_code` のトンネル／橋・高架、`vt_lvorder` の階層順）が入っている。
  藤沢版では PLATEAU（約300都市）と OSM の幾何推定しか手が無かった部分である。

━━ 一次情報 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  配信   : https://cyberjapandata.gsi.go.jp/xyz/optimal_bvmap-v1/{z}/{x}/{y}.pbf
  仕様書 : https://maps.gsi.go.jp/help/pdf/vector/optbv_attribute.pdf
  リポジトリ: https://github.com/gsi-cyberjapan/optimal_bvmap
  ズーム : 4〜16（z17 の情報は z16 に含む）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

★**試験公開である。** 国土地理院自身が「URL やデータ構成、データの内容
  （属性の有無や名称等）が変わる可能性があります」と明記している。
  **必須要件の土台にしてよいかは P-4 の結果を見て判断する。**

★**`vt_width` は使わない。** 仕様書が「地図表示用の仮想的な幅員」と明記しており、
  実測値ではない。使ってよいのは区分である `vt_rnkwidth` のほう。

★取得は**絶対ルール3**に従う: 逐次・sleep・同時接続1・ローカルキャッシュ・UA 明示。
  同じタイルを二度取りに行かない。
"""

from __future__ import annotations

import http.client
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

TILE_URL = "https://cyberjapandata.gsi.go.jp/xyz/optimal_bvmap-v1/{z}/{x}/{y}.pbf"
MAX_ZOOM = 16
ROAD_LAYER = "RdCL"  # 道路中心線

#: 地物種別コード（公式の地物コード表）。★橋とトンネルは T-01 の手がかりになる。
CODE_ROAD_NORMAL = 2701      # 道路中心線・通常部
CODE_ROAD_BRIDGE = 2703      # 道路中心線・橋・高架
CODE_ROAD_TUNNEL = 2704      # 道路中心線・トンネル
CODE_DUAL_NORMAL = 2711      # ２条道路中心線・通常部
CODE_DUAL_SNOWSHED = 2712    # ２条道路中心線・雪覆い等
CODE_DUAL_BRIDGE = 2713      # ２条道路中心線・橋・高架
CODE_DUAL_TUNNEL = 2714      # ２条道路中心線・トンネル

BRIDGE_CODES = frozenset({CODE_ROAD_BRIDGE, CODE_DUAL_BRIDGE})
TUNNEL_CODES = frozenset({CODE_ROAD_TUNNEL, CODE_DUAL_TUNNEL})

#: `vt_rnkwidth` の値（実データで確認した表記）と、その区分が表す幅［m］の範囲。
#: ★上端・下端が無い区分は None。**中央値で埋めない**（絶対ルール10）。
RNKWIDTH_RANGE: dict[str, tuple[float | None, float | None]] = {
    "3m未満": (None, 3.0),
    "3m-5.5m未満": (3.0, 5.5),
    "5.5m-13m未満": (5.5, 13.0),
    "13m-19.5m未満": (13.0, 19.5),
    "19.5m以上": (19.5, None),
}
#: ★区分が付かない場合の値。実データに現れたら記録する（推測で埋めない）。
RNKWIDTH_UNKNOWN = frozenset({"その他", "不明", None, ""})


# -----------------------------------------------------------------------------
# タイル番号 ↔ 経緯度
# -----------------------------------------------------------------------------
def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2**z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return x, y


def tiles_for_bbox(
    west: float, south: float, east: float, north: float, z: int
) -> list[tuple[int, int]]:
    """bbox を覆うタイル番号の一覧（左上→右下の順）。"""
    x0, y0 = lonlat_to_tile(west, north, z)
    x1, y1 = lonlat_to_tile(east, south, z)
    return [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]


def tile_to_lonlat(x: int, y: int, z: int) -> tuple[float, float]:
    """タイル左上角の経緯度。"""
    n = 2**z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lon, lat


# -----------------------------------------------------------------------------
# 取得
# -----------------------------------------------------------------------------
@dataclass
class VectorTiles:
    """逐次・sleep・キャッシュ付きのタイル取得器（絶対ルール3）。

    ★`cache_dir` に置いたタイルは二度取りに行かない。
    ★404（そのタイルにデータが無い）は **0バイトの印**として記録する。
      印を残さないと、データの無い区画を毎回取りに行くことになる。
    """

    cache_dir: Path
    user_agent: str
    sleep_sec: float = 1.0
    #: ★一時的な失敗（5xx・タイムアウト・接続断）のやり直し。config の tiles.max_retries 等
    #   やり直さないと、全国で数十万回のうち1回の 502 で県がまるごと連鎖から落ちる
    #   （2026-09-23 のレビューで指摘）。
    max_retries: int = 3
    retry_backoff_sec: float = 2.0
    #: ★429/503 の扱いは kansui.fetcher と同じ（config の tiles 節）。
    #   sleep を 0 にしたので、合図に従う仕組みが無いと詰めて送り続ける（6回目のレビューで指摘）
    sleep_after_throttle_sec: float = 1.0
    max_retry_after_sec: float = 900.0
    _fetched: int = field(default=0, init=False)
    _cache_hits: int = field(default=0, init=False)
    _missing: int = field(default=0, init=False)
    _bytes: int = field(default=0, init=False)

    @property
    def stats(self) -> dict[str, int]:
        return {
            "fetched": self._fetched,
            "cache_hits": self._cache_hits,
            "missing": self._missing,
            "bytes_downloaded": self._bytes,
        }

    def _path(self, z: int, x: int, y: int) -> Path:
        return self.cache_dir / f"optimal_bvmap-v1/{z}/{x}/{y}.pbf"

    def fetch(self, z: int, x: int, y: int) -> bytes | None:
        """1タイル取得。データが無いタイルは None。"""
        p = self._path(z, x, y)
        if p.exists():
            self._cache_hits += 1
            data = p.read_bytes()
            return data or None

        url = TILE_URL.format(z=z, x=x, y=y)
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=60) as res:  # noqa: S310
                    data = res.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    data = b""  # ★「無い」ことをキャッシュする
                    self._missing += 1
                    break
                if e.code in (429, 503):
                    from kansui.fetcher import _retry_after_sec, note_throttled

                    self.sleep_sec = max(self.sleep_sec, self.sleep_after_throttle_sec)
                    note_throttled(self.cache_dir)
                    if attempt >= self.max_retries:
                        raise
                    wait = _retry_after_sec(e.headers.get("Retry-After") if e.headers else None)
                    time.sleep(min(max(wait or 0.0, self.retry_backoff_sec * (2**attempt)),
                                   self.max_retry_after_sec))
                    continue
                if attempt >= self.max_retries or e.code < 500:
                    raise
            # ★http.client.HTTPException（IncompleteRead・BadStatusLine）は OSError の子ではない。
            #   本文の途中で切れたときにやり直さずに落ちていた（2026-09-23 のレビューで指摘）
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError,
                    http.client.HTTPException):
                if attempt >= self.max_retries:
                    raise
            else:
                self._fetched += 1
                self._bytes += len(data)
                break
            # 指数バックオフ（標高タイルの取得器と同じ）。遮断された場合に畳みかけないため
            time.sleep(self.retry_backoff_sec * (2**attempt))

        # ★一時ファイルに書いてから置き換える。途中で止まると 0 バイトのファイルが残り、
        #   **永久に「タイルが無い（404）」と読まれる**（空き不足・強制終了のとき）。
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(p)
        # ★キャッシュに当たったときは待たない。待つのは実際に叩いたときだけ。
        time.sleep(self.sleep_sec)
        return data or None


# -----------------------------------------------------------------------------
# 復号
# -----------------------------------------------------------------------------
def decode_layer(
    data: bytes, z: int, x: int, y: int, layer: str = ROAD_LAYER
) -> list[tuple[list[list[tuple[float, float]]], dict]]:
    """1タイルの1レイヤを (経緯度のラインの集まり, 属性) の列にして返す。

    ★MVT の座標はタイル内のローカル格子（既定 extent=4096）なので、
      **必ず経緯度へ直してから**使う。ここを飛ばすと距離計算が静かに壊れる。
    """
    import mapbox_vector_tile as mvt

    tile = mvt.decode(data)
    lyr = tile.get(layer)
    if not lyr:
        return []
    extent = lyr.get("extent", 4096)
    n = 2**z

    def to_lonlat(px: float, py: float) -> tuple[float, float]:
        # mapbox_vector_tile は既定で y を下から上へ直して返す（y_coord_down=False）
        wx = (x + px / extent) / n
        wy = (y + (1.0 - py / extent)) / n
        lon = wx * 360.0 - 180.0
        lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * wy))))
        return lon, lat

    out = []
    for feat in lyr["features"]:
        geom = feat["geometry"]
        gtype = geom["type"]
        if gtype == "LineString":
            parts = [geom["coordinates"]]
        elif gtype == "MultiLineString":
            parts = geom["coordinates"]
        else:
            continue
        lines = [[to_lonlat(px, py) for px, py in part] for part in parts if len(part) >= 2]
        if lines:
            out.append((lines, feat["properties"]))
    return out


def tile_bbox_of(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    """タイルの (west, south, east, north)［度］。"""
    w, n = tile_to_lonlat(x, y, z)
    e, s = tile_to_lonlat(x + 1, y + 1, z)
    return w, s, e, n
