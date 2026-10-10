/**
 * ★TypeScript の区画コード計算が、前処理（Python）と**同じ結果**になるかを突き合わせる。
 *
 * ★★ここがずれると黙って壊れる。
 *   存在しない区画を要求すれば 404 で気づけるが、
 *   **隣の区画を読んでしまうと経路が区画の縁で切れるだけ**で、例外は出ない。
 *
 * 使い方:
 *   npx esbuild --bundle web/tools/mesh-crosscheck.mjs --format=esm --outfile=/tmp/cc.mjs
 *   node /tmp/cc.mjs > /tmp/ts_mesh.json
 *   cd preprocess && uv run python tools/mesh_crosscheck.py
 *
 * ★2026-09-21 の実績: 12,063件（区画の角ちょうどを含む）すべて一致。
 */
import { meshCode, meshBbox, meshesForBbox, meshNeighbors } from "../src/mesh.ts";
const pts = [];
// 実データの範囲を広くなめる（藤沢〜神奈川〜北海道の境界まで）
let seed = 20260921;
const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
for (let i = 0; i < 4000; i++) {
  pts.push([122 + rnd() * 32, 20 + rnd() * 26]);
}
// 区画の角ちょうど（★いちばん危ない）
for (const c of ["5339", "533946", "53394600", "523972", "533903", "644142", "392000"]) {
  const [w, s, e, n] = meshBbox(c);
  pts.push([w, s], [e - 1e-9, n - 1e-9], [(w + e) / 2, (s + n) / 2]);
}
const out = [];
for (const [lon, lat] of pts) for (const lv of [1, 2, 3]) out.push([lon, lat, lv, meshCode(lon, lat, lv)]);
const boxes = [];
for (const b of [[139.40,35.30,139.51,35.43],[139.99,35.66,140.01,35.68],[140.98,43.0,141.35,43.22]])
  boxes.push([b, meshesForBbox(b[0], b[1], b[2], b[3], 2)]);
// ★隣の区画も照合する（1次の桁で繰り上がるところが危ない）
const nbs = [];
for (const c of ["5339","533946","533907","523972","533903","644142","392000","533900","533977"])
  nbs.push([c, meshNeighbors(c, c.length / 2 - 1)]);
console.log(JSON.stringify({ out, boxes, nbs }));
