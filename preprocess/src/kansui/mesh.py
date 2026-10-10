"""標準地域メッシュ（1次／2次／3次）のコードと範囲。

★全国版は道路グラフを**地域ごとに分割して配る**必要がある（NK-06）。
  その単位の第1候補が **2次地域区画（約10km四方）**。
  理由: 全国で番号体系が定義済みで、**県境にも平面直角座標系の境界にも依存しない。**

━━ 一次情報 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  総務省統計局「地域メッシュ統計について」 https://www.stat.go.jp/data/mesh/m_tuite.html
  区分方法（PDF） https://www.stat.go.jp/data/mesh/pdf/m-kukaku.pdf
  根拠: **昭和48年7月12日行政管理庁告示第143号**（全国地域メッシュ）
  取得日: 2026年9月20日

  第1次地域区画  緯度 40分 × 経度 1度      およそ 80km 四方
  第2次地域区画  緯度  5分 × 経度 7分30秒   およそ 10km 四方（1次を 8×8 に分割）
  第3次地域区画  緯度 30秒 × 経度 45秒      およそ  1km 四方（2次を 10×10 に分割）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

★**コードは文字列で扱う。** 3次メッシュには `53394600` のように 0 が並ぶものがあり、
  数値にすると桁が落ちる。T-16（YAML の 0 始まりが8進数になる）と同じ筋の事故。

★式は公式 PDF の例「**5339-46 ＝ 千代田区**」で検算してある（テストに固定）。

★**区画の大きさは「秒」の整数で持つ。度の割り算で持ってはいけない。**
  2026-09-20 に実際に踏んだ: 1次区画 `5339` の南端を度で作ると
  `35.33333333333333` になり、`×60` すると `2119.9999999999995` で、
  切り捨てが **1つ下の区画 `5239`** を指した。
  **区画の縁の道路が隣の区画に入る**＝経路が切れる形の事故になる。
  → 経緯度を**秒に直し、丸めてから整数で割る。**
"""

from __future__ import annotations

#: 区画の大きさ［秒］。★すべて整数で割り切れる（度では割り切れない）
LAT1_SEC, LON1_SEC = 40 * 60, 60 * 60      # 1次: 緯度40分 × 経度1度
LAT2_SEC, LON2_SEC = 5 * 60, 450           # 2次: 緯度5分 × 経度7分30秒
LAT3_SEC, LON3_SEC = 30, 45                # 3次: 緯度30秒 × 経度45秒

#: 度に直した大きさ（呼び出し側の見積もり用。内部の判定には使わない）
LAT1, LON1 = LAT1_SEC / 3600, LON1_SEC / 3600
LAT2, LON2 = LAT2_SEC / 3600, LON2_SEC / 3600
LAT3, LON3 = LAT3_SEC / 3600, LON3_SEC / 3600

DIV2, DIV3 = 8, 10

#: レベル → (コードの桁数, 緯度［秒］, 経度［秒］)
_LEVELS = {
    1: (4, LAT1_SEC, LON1_SEC),
    2: (6, LAT2_SEC, LON2_SEC),
    3: (8, LAT3_SEC, LON3_SEC),
}

#: ★度→秒に直したときの表現誤差を吸収する丸め桁。
#   1e-6 秒 ≒ 3×10⁻⁸ m。実際の誤差（1e-11 秒程度）より十分大きく、
#   現実の位置精度より十分小さい。
_SEC_ROUND = 6


def _to_sec(deg: float) -> float:
    return round(deg * 3600.0, _SEC_ROUND)


class MeshCodeError(ValueError):
    """メッシュコードとして受け付けられない。★黙って丸めないための例外。"""


def _check(code: str) -> int:
    """コードを検査してレベルを返す。"""
    if not isinstance(code, str):
        raise MeshCodeError(f"メッシュコードは文字列: {code!r}（数値にすると 0 が落ちる）")
    # ★`\d` を使わない。Python の `\d` は全角数字にも一致する（藤沢版 T-16）
    if not code or not all(c in "0123456789" for c in code):
        raise MeshCodeError(f"半角数字のみ: {code!r}")
    for level, (digits, _, _) in _LEVELS.items():
        if len(code) == digits:
            return level
    raise MeshCodeError(f"桁数が 4/6/8 のいずれでもない: {code!r}（{len(code)} 桁）")


def mesh_code(lon: float, lat: float, level: int = 2) -> str:
    """経緯度 → メッシュコード。

    ★南西の角を含み、北東の角を含まない（半開区間）。境界上の点は
      **北東側ではなく南西側の区画**に入る。
    """
    if level not in _LEVELS:
        raise MeshCodeError(f"レベルは 1/2/3: {level}")
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise MeshCodeError(f"経緯度が範囲外: {lon}, {lat}")

    # ★度のまま割らない。秒に直して丸めてから整数で割る（先頭の注意書き参照）
    lat_sec, lon_sec = _to_sec(lat), _to_sec(lon)

    p = int(lat_sec // LAT1_SEC)
    u = int(lon_sec // LON1_SEC) - 100
    if not (0 <= u <= 99):
        raise MeshCodeError(f"日本の範囲外の経度: {lon}（1次コードが 2桁に収まらない）")
    code = f"{p:02d}{u:02d}"
    if level == 1:
        return code

    lat_rest = lat_sec - p * LAT1_SEC      # ［秒］
    lon_rest = lon_sec - (u + 100) * LON1_SEC
    q = int(lat_rest // LAT2_SEC)          # 0〜7（南 → 北）
    v = int(lon_rest // LON2_SEC)          # 0〜7（西 → 東）
    code += f"{q}{v}"
    if level == 2:
        return code

    r = int((lat_rest - q * LAT2_SEC) // LAT3_SEC)   # 0〜9
    w = int((lon_rest - v * LON2_SEC) // LON3_SEC)   # 0〜9
    return code + f"{r}{w}"


def mesh_bbox(code: str) -> tuple[float, float, float, float]:
    """メッシュコード → (west, south, east, north)［度］。"""
    level = _check(code)
    # ★秒の整数で組み立ててから度に直す。度で足し込むと縁がずれる
    lat_sec = int(code[0:2]) * LAT1_SEC
    lon_sec = (int(code[2:4]) + 100) * LON1_SEC
    if level >= 2:
        lat_sec += int(code[4]) * LAT2_SEC
        lon_sec += int(code[5]) * LON2_SEC
    if level >= 3:
        lat_sec += int(code[6]) * LAT3_SEC
        lon_sec += int(code[7]) * LON3_SEC
    _, dlat_sec, dlon_sec = _LEVELS[level]
    return (
        lon_sec / 3600.0,
        lat_sec / 3600.0,
        (lon_sec + dlon_sec) / 3600.0,
        (lat_sec + dlat_sec) / 3600.0,
    )


def meshes_for_bbox(
    west: float, south: float, east: float, north: float, level: int = 2
) -> list[str]:
    """bbox に重なるメッシュコードの一覧（南西 → 北東の順）。"""
    if level not in _LEVELS:
        raise MeshCodeError(f"レベルは 1/2/3: {level}")
    _, dlat_sec, dlon_sec = _LEVELS[level]
    sw = mesh_bbox(mesh_code(west, south, level))
    sw_lon_sec, sw_lat_sec = _to_sec(sw[0]), _to_sec(sw[1])
    # ★半開区間なので、北東端が区画の角ちょうどのときに1列ぶん増やさない
    #   `-(-a // b)` は切り上げ除算。差が 0 でも 1 区画は返す
    n_lon = max(1, -(-int(_to_sec(east) - sw_lon_sec) // dlon_sec))
    n_lat = max(1, -(-int(_to_sec(north) - sw_lat_sec) // dlat_sec))
    return [
        mesh_code(
            (sw_lon_sec + (j + 0.5) * dlon_sec) / 3600.0,
            (sw_lat_sec + (i + 0.5) * dlat_sec) / 3600.0,
            level,
        )
        for i in range(n_lat)
        for j in range(n_lon)
    ]


def parent(code: str) -> str:
    """1つ上のレベルのコード。1次には親が無いので MeshCodeError。"""
    level = _check(code)
    if level == 1:
        raise MeshCodeError("1次地域区画に親は無い")
    return code[: _LEVELS[level - 1][0]]


def neighbors(code: str, ring: int = 1) -> list[str]:
    """周囲のメッシュコード（自分を含まない）。

    ★P-2 で「経路に要る区画だけ読む」ときに使う。区画の境界をまたぐ経路のため、
      **隣を読めること**が要件（設計 §5 P-2）。
    ★コードの数値に 1 を足して隣を求めてはいけない（1次の桁で繰り上がるため）。
      中心の経緯度から足し引きして引き直す。
    """
    level = _check(code)
    _, dlat_sec, dlon_sec = _LEVELS[level]
    w, s, e, n = mesh_bbox(code)
    clon_sec, clat_sec = _to_sec((w + e) / 2), _to_sec((s + n) / 2)
    out = []
    for i in range(-ring, ring + 1):
        for j in range(-ring, ring + 1):
            if i == 0 and j == 0:
                continue
            out.append(mesh_code(
                (clon_sec + j * dlon_sec) / 3600.0,
                (clat_sec + i * dlat_sec) / 3600.0,
                level,
            ))
    return out


def cover3_masks(lon, lat) -> dict[str, str]:
    """点の集まり → 2次区画ごとに「点を含む3次区画（約1km四方）」の100桁のビット列。

    ★対象範囲を**道路データから**描くために使う（→ build_region / クライアントの coverage）。
      行政区域（N03）の形は配らない。国土数値情報の N03 は
      「測量法に基づく国土地理院長承認（複製）」の上に成り立っており、
      **二次利用に国土地理院への申請が要る場合がある**と公式ページが明記している
      （2026-09-23 確認）。道路（OSM）と標準地域メッシュの番号だけで作れば、その論点を持ち込まない。

    ★丸めは `mesh_code` と同じ（秒の整数・小数6桁で丸める）。**食い違うと
      クライアントの判定とずれる**（→ web/src/mesh.ts）。

    返り値: {"533945": "0010...（100文字）", ...}。i 文字目 = 3次の (r=i//10, w=i%10)。
    """
    import numpy as np

    x = np.asarray(lon, dtype=np.float64).ravel()
    y = np.asarray(lat, dtype=np.float64).ravel()
    lat_s = np.round(y * 3600.0, _SEC_ROUND)
    lon_s = np.round(x * 3600.0, _SEC_ROUND)
    p = np.floor(lat_s / LAT1_SEC).astype(np.int64)
    u = np.floor(lon_s / LON1_SEC).astype(np.int64) - 100
    lr = lat_s - p * LAT1_SEC
    lo = lon_s - (u + 100) * LON1_SEC
    q = np.floor(lr / LAT2_SEC).astype(np.int64)
    v = np.floor(lo / LON2_SEC).astype(np.int64)
    r = np.clip(np.floor((lr - q * LAT2_SEC) / 30.0).astype(np.int64), 0, 9)
    w = np.clip(np.floor((lo - v * LON2_SEC) / 45.0).astype(np.int64), 0, 9)
    code2 = p * 10000 + u * 100 + q * 10 + v
    key = np.unique(code2 * 100 + r * 10 + w)
    out: dict[str, list[str]] = {}
    for k in key.tolist():
        c2, bit = divmod(k, 100)
        s2 = f"{c2:06d}"
        out.setdefault(s2, ["0"] * 100)[bit] = "1"
    return {c: "".join(b) for c, b in out.items()}


def mesh2_codes(lon, lat):
    """経緯度の配列 → 2次メッシュコードの int64 配列（★`mesh_code(level=2)` と同じ丸め）。

    ★書き出しで数千万点を処理するための配列版。1点ずつ文字列を作らない。
    """
    import numpy as np

    x = np.asarray(lon, dtype=np.float64)
    y = np.asarray(lat, dtype=np.float64)
    lat_s = np.round(y * 3600.0, _SEC_ROUND)
    lon_s = np.round(x * 3600.0, _SEC_ROUND)
    p = np.floor(lat_s / LAT1_SEC).astype(np.int64)
    u = np.floor(lon_s / LON1_SEC).astype(np.int64) - 100
    if ((u < 0) | (u > 99)).any():
        raise MeshCodeError("日本の範囲外の経度がある（1次コードが 2桁に収まらない）")
    q = np.floor((lat_s - p * LAT1_SEC) / LAT2_SEC).astype(np.int64)
    v = np.floor((lon_s - (u + 100) * LON1_SEC) / LON2_SEC).astype(np.int64)
    return p * 10000 + u * 100 + q * 10 + v
