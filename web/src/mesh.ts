/**
 * 標準地域メッシュのコード計算（★前処理側 `kansui/mesh.py` と**必ず同じ結果**にすること）。
 *
 * 根拠: 昭和48年7月12日行政管理庁告示第143号
 *   第1次地域区画  緯度 40分 × 経度 1度      およそ 80km 四方
 *   第2次地域区画  緯度  5分 × 経度 7分30秒   およそ 10km 四方
 *   第3次地域区画  緯度 30秒 × 経度 45秒      およそ  1km 四方
 *
 * ★★**区画の大きさは「秒」の整数で持つ。度の割り算で持ってはいけない。**
 *   前処理側で 2026-09-20 に実際に踏んだ:
 *   1次区画 `5339` の南端を度で作ると `35.33333333333333` になり、
 *   `×60` すると `2119.9999999999995` で、切り捨てが **1つ下の区画 `5239`** を指した。
 *   → **区画の縁の道路が隣の区画に入る**＝経路が切れる形の事故になる。
 *
 * ★★**ここが前処理とずれると、クライアントが存在しない区画を要求する。**
 *   404 になれば気づけるが、**隣の区画を読んでしまうと黙って経路が切れる。**
 *   ★実際に突き合わせているのは `web/tools/route_probe.mjs`（配布データに対して
 *   `homeOf` → 区画の取得まで通す）と `preprocess/tools/verify_mesh_merge.py`。
 *   **web 側に単体テストの仕組みは無い。**
 */

const LAT1_SEC = 40 * 60;
const LON1_SEC = 60 * 60;
const LAT2_SEC = 5 * 60;
const LON2_SEC = 450;
const LAT3_SEC = 30;
const LON3_SEC = 45;

const LEVELS: Record<number, [number, number, number]> = {
  1: [4, LAT1_SEC, LON1_SEC],
  2: [6, LAT2_SEC, LON2_SEC],
  3: [8, LAT3_SEC, LON3_SEC],
};

/** ★度→秒に直したときの表現誤差を吸収する。1e-6 秒 ≒ 3×10⁻⁸ m */
function toSec(deg: number): number {
  return Math.round(deg * 3600 * 1e6) / 1e6;
}

const pad2 = (n: number) => (n < 10 ? `0${n}` : `${n}`);

export function meshCode(lon: number, lat: number, level = 2): string {
  const latSec = toSec(lat);
  const lonSec = toSec(lon);
  const p = Math.floor(latSec / LAT1_SEC);
  const u = Math.floor(lonSec / LON1_SEC) - 100;
  let code = `${pad2(p)}${pad2(u)}`;
  if (level === 1) return code;

  const latRest = latSec - p * LAT1_SEC;
  const lonRest = lonSec - (u + 100) * LON1_SEC;
  const q = Math.floor(latRest / LAT2_SEC);
  const v = Math.floor(lonRest / LON2_SEC);
  code += `${q}${v}`;
  if (level === 2) return code;

  const r = Math.floor((latRest - q * LAT2_SEC) / LAT3_SEC);
  const w = Math.floor((lonRest - v * LON2_SEC) / LON3_SEC);
  return code + `${r}${w}`;
}

/** メッシュコード → [west, south, east, north]（度） */
export function meshBbox(code: string): [number, number, number, number] {
  const level = code.length / 2 - 1;
  const [, dLat, dLon] = LEVELS[level];
  let latSec = Number(code.slice(0, 2)) * LAT1_SEC;
  let lonSec = (Number(code.slice(2, 4)) + 100) * LON1_SEC;
  if (level >= 2) {
    latSec += Number(code[4]) * LAT2_SEC;
    lonSec += Number(code[5]) * LON2_SEC;
  }
  if (level >= 3) {
    latSec += Number(code[6]) * LAT3_SEC;
    lonSec += Number(code[7]) * LON3_SEC;
  }
  return [lonSec / 3600, latSec / 3600, (lonSec + dLon) / 3600, (latSec + dLat) / 3600];
}

/** bbox に重なる区画コードの一覧（南西 → 北東） */
export function meshesForBbox(
  west: number,
  south: number,
  east: number,
  north: number,
  level = 2,
): string[] {
  const [, dLat, dLon] = LEVELS[level];
  const sw = meshBbox(meshCode(west, south, level));
  const swLon = toSec(sw[0]);
  const swLat = toSec(sw[1]);
  // ★半開区間なので、北東端が区画の角ちょうどのときに1列ぶん増やさない
  const nLon = Math.max(1, Math.ceil(Math.trunc(toSec(east) - swLon) / dLon));
  const nLat = Math.max(1, Math.ceil(Math.trunc(toSec(north) - swLat) / dLat));
  const out: string[] = [];
  for (let i = 0; i < nLat; i++) {
    for (let j = 0; j < nLon; j++) {
      out.push(meshCode((swLon + (j + 0.5) * dLon) / 3600, (swLat + (i + 0.5) * dLat) / 3600, level));
    }
  }
  return out;
}

/**
 * 周囲の区画コード（自分を含まない）。
 *
 * ★コードの数値に 1 を足して隣を求めてはいけない（1次の桁で繰り上がる）。
 *   中心の経緯度から足し引きして引き直す。前処理側 `mesh.py` の `neighbors` と同じ。
 */
export function meshNeighbors(code: string, level = 2): string[] {
  const [, dLat, dLon] = LEVELS[level];
  const [w, s, e, n] = meshBbox(code);
  const cLon = toSec((w + e) / 2);
  const cLat = toSec((s + n) / 2);
  const out: string[] = [];
  for (let i = -1; i <= 1; i++) {
    for (let j = -1; j <= 1; j++) {
      if (i === 0 && j === 0) continue;
      out.push(meshCode((cLon + j * dLon) / 3600, (cLat + i * dLat) / 3600, level));
    }
  }
  return out;
}

