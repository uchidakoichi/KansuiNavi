/**
 * 対象範囲（道路データがある範囲）の判定と外周線。★地図の破線とワーカーの「範囲外」判定は
 * **必ずこのモジュールの同じ結果**を使う。線の内外と判定が食い違わないように。
 *
 * ★★範囲の形は**道路（OSM）から**作る。行政区域（N03）の形は配らない。
 *   国土数値情報の N03 は「測量法に基づく国土地理院長承認（複製）」の上に成り立ち、
 *   **二次利用に国土地理院への申請が要る場合がある**と公式ページが明記している（2026-09-23 確認）。
 *
 * 元データ: `index.cover3` ＝ 2次区画ごとに「道路の端点を含む3次区画（約1km四方）」の
 *   100桁のビット列（i 文字目 = 3次の r=i/10, w=i%10）。前処理 `kansui.mesh.cover3_masks`。
 *
 * ★**穴を埋める。** 周りを道路に囲まれた空白（大きな公園など）は「内側」とする。
 *   → 外側から道路の無い格子をたどって塗り、**たどり着けなかった格子を「内側」**とする。
 * ★★ただし**外へ開いた空白は埋まらない**（2026-09-23 に実測）。
 *   丹沢の道路の無い一帯は県境（山梨側）まで続いていて、穴ではなく「入り江」になる。
 *   → 丹沢山の山頂は「外」になる。**それは「道路データが無い」という事実**なので、
 *     ワーカーの文言は「範囲の外」ではなく「この周辺には道路データがありません」にしてある。
 *
 * ★古い配布データ（`cover3` が無い）では2次区画の集合に落とす（粗いが外れない側）。
 */

import { meshBbox, meshCode } from "./mesh";

const LAT3 = 30; // 3次区画の大きさ［秒］
const LON3 = 45;

export interface Coverage {
  /** その点が対象範囲の内側か */
  contains(lon: number, lat: number): boolean;
  /** 外周の線分（度） */
  outline: [number, number][][];
  /** 何で作ったか（"cover3" = 1km 格子 / "mesh2" = 2次区画） */
  source: "cover3" | "mesh2";
}

interface IndexLike {
  meshes: Record<string, unknown>;
  cover3?: Record<string, string>;
  /** 閉じ処理の半径［格子］。config の `coverage.closing_cells` から来る */
  coverClosing?: number;
  meshLevel: number;
}

function cellOf(lon: number, lat: number): [number, number] {
  // ★mesh.ts / mesh.py と同じ丸め（秒・小数6桁）で、秒の整数の格子に落とす
  const latSec = Math.round(lat * 3600 * 1e6) / 1e6;
  const lonSec = Math.round(lon * 3600 * 1e6) / 1e6;
  return [Math.floor(latSec / LAT3), Math.floor(lonSec / LON3)];
}

export function buildCoverage(index: IndexLike): Coverage {
  if (!index.cover3) return fromMesh2(index);
  const k = Math.max(0, Math.floor(index.coverClosing ?? 0));

  // 1) 道路のある 1km 格子（行・列は秒の整数を格子の大きさで割ったもの）
  const road: [number, number][] = [];
  let rMin = Infinity, rMax = -Infinity, cMin = Infinity, cMax = -Infinity;
  for (const [code2, bits] of Object.entries(index.cover3)) {
    const [w, s] = meshBbox(code2);
    const row0 = Math.round((s * 3600) / LAT3);
    const col0 = Math.round((w * 3600) / LON3);
    for (let i = 0; i < 100; i++) {
      if (bits[i] !== "1") continue;
      const row = row0 + Math.floor(i / 10);
      const col = col0 + (i % 10);
      road.push([row, col]);
      rMin = Math.min(rMin, row); rMax = Math.max(rMax, row);
      cMin = Math.min(cMin, col); cMax = Math.max(cMax, col);
    }
  }
  // 閉じ処理と外側からのたどりのために、枠を k+1 格子ぶん広げる
  const pad = k + 1;
  rMin -= pad; rMax += pad; cMin -= pad; cMax += pad;
  const H = rMax - rMin + 1, W = cMax - cMin + 1;
  const at = (r: number, c: number) => (r - rMin) * W + (c - cMin);
  let grid = new Uint8Array(H * W);
  for (const [r, c] of road) grid[at(r, c)] = 1;

  // 2) ★閉じ処理（膨張 k → 収縮 k）。道路と道路の間の**小さな切れ込み**を埋め、
  //   外周のギザギザを均す。凸な縁は収縮で元へ戻るので、範囲が外へ太ることは無い。
  //   ★大きな空白（丹沢・奥多摩）は埋まらない。そこは「道路データが無い」のが事実。
  if (k > 0) {
    // ★正方形の窓の膨張・収縮は**横と縦の2回に分けられる**（結果は同じ）。さらに窓の中の 1 の数を
    //   **累積和**で数えるので、1セルあたりの手間は窓の大きさによらない。
    //   窓全体を毎回見ると全国（約 540万セル × 25）で 669ms かかり、起動時にメインスレッドが止まった
    //   （2026-09-24 に合成の全国データで測定）。
    //   ★枠の外は 0（道路なし）とみなす。膨張では何も足さず、収縮では 0 に倒す（従来と同じ）。
    const win = 2 * k + 1;
    const keep = (ones: number, want: number) =>
      want === 1 ? (ones > 0 ? 1 : 0) : ones === win ? 1 : 0;
    // 横: 行ごとに窓の件数を足し引きしながら進む
    const horiz = (src: Uint8Array, want: number) => {
      const dst = new Uint8Array(H * W);
      for (let r = 0; r < H; r++) {
        const b = r * W;
        let ones = 0;
        for (let c = 0; c <= k && c < W; c++) ones += src[b + c];
        for (let c = 0; c < W; c++) {
          dst[b + c] = keep(ones, want);
          if (c + k + 1 < W) ones += src[b + c + k + 1];
          if (c - k >= 0) ones -= src[b + c - k];
        }
      }
      return dst;
    };
    // 縦: 列ごとの窓の件数を持ち、行の順に読みながら足し引きする（★飛び飛びに読まない）
    const vert = (src: Uint8Array, want: number) => {
      const dst = new Uint8Array(H * W);
      const cnt = new Int32Array(W);
      for (let r = 0; r <= k && r < H; r++) for (let c = 0; c < W; c++) cnt[c] += src[r * W + c];
      for (let r = 0; r < H; r++) {
        const b = r * W;
        for (let c = 0; c < W; c++) dst[b + c] = keep(cnt[c], want);
        if (r + k + 1 < H) {
          const a = (r + k + 1) * W;
          for (let c = 0; c < W; c++) cnt[c] += src[a + c];
        }
        if (r - k >= 0) {
          const d = (r - k) * W;
          for (let c = 0; c < W; c++) cnt[c] -= src[d + c];
        }
      }
      return dst;
    };
    const morph = (src: Uint8Array, want: number) => vert(horiz(src, want), want);
    grid = morph(morph(grid, 1), 0); // 膨張（近くに 1 があれば 1）→ 収縮（近くに 0 があれば 0）
  }

  // 3) ★穴を埋める。外側から「範囲でない格子」をたどり、たどり着けなかったものは内側
  // ★積み上げは型付き配列で持ち、隣を見るたびに配列を作らない（全国では海の上を含め数百万セル）
  const outsideMark = new Uint8Array(H * W);
  const stack = new Int32Array(H * W);
  let sp = 0;
  stack[sp++] = 0;
  outsideMark[0] = 1;
  const visit = (j: number) => {
    if (outsideMark[j] || grid[j]) return;
    outsideMark[j] = 1;
    stack[sp++] = j;
  };
  while (sp > 0) {
    const i = stack[--sp];
    const r = (i / W) | 0;
    const c = i - r * W;
    if (r + 1 < H) visit(i + W);
    if (r > 0) visit(i - W);
    if (c + 1 < W) visit(i + 1);
    if (c > 0) visit(i - 1);
  }
  const inside = (row: number, col: number) =>
    row >= rMin && row <= rMax && col >= cMin && col <= cMax && !outsideMark[at(row, col)];

  // 4) 外周: 内側の格子の辺のうち、隣が内側でないもの
  const outline: [number, number][][] = [];
  const deg = (row: number, col: number): [number, number] => [(col * LON3) / 3600, (row * LAT3) / 3600];
  for (let r = rMin; r <= rMax; r++) {
    for (let c = cMin; c <= cMax; c++) {
      if (!inside(r, c)) continue;
      if (!inside(r - 1, c)) outline.push([deg(r, c), deg(r, c + 1)]);
      if (!inside(r + 1, c)) outline.push([deg(r + 1, c), deg(r + 1, c + 1)]);
      if (!inside(r, c - 1)) outline.push([deg(r, c), deg(r + 1, c)]);
      if (!inside(r, c + 1)) outline.push([deg(r, c + 1), deg(r + 1, c + 1)]);
    }
  }

  return {
    source: "cover3",
    outline,
    contains(lon, lat) {
      const [row, col] = cellOf(lon, lat);
      return inside(row, col);
    },
  };
}

/** 古い配布データ向け。2次区画の集合そのもの（粗いが「外」を内と言うことは無い…縁の区画を除いて） */
function fromMesh2(index: IndexLike): Coverage {
  const codes = Object.keys(index.meshes);
  const set = new Set(codes);
  const count = new Map<string, number>();
  const seg = new Map<string, [number, number, number, number]>();
  for (const code of codes) {
    const [w, s, e, n] = meshBbox(code).map((v) => Math.round(v * 3600));
    for (const [a, b, c, d] of [[w, s, e, s], [e, s, e, n], [w, n, e, n], [w, s, w, n]]) {
      const k = `${a},${b},${c},${d}`;
      count.set(k, (count.get(k) ?? 0) + 1);
      seg.set(k, [a, b, c, d]);
    }
  }
  const outline: [number, number][][] = [];
  for (const [k, c] of count) {
    if (c !== 1) continue;
    const [a, b, cc, d] = seg.get(k)!;
    outline.push([[a / 3600, b / 3600], [cc / 3600, d / 3600]]);
  }
  return {
    source: "mesh2",
    outline,
    contains: (lon, lat) => set.has(meshCode(lon, lat, index.meshLevel)),
  };
}
