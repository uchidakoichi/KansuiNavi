/**
 * ★**ビルド済みのワーカーそのもの**をブラウザ抜きで動かし、経路の挙動を測る。
 *
 * なぜ要るか（2026-09-21）:
 *   経路の挙動（どの区画を読むか・何ノード確定するか・断るべきものを断るか）は、
 *   **クリックの当たり判定とは別の話**なのに、確認手段が実機のタップしか無かった。
 *   実際その日、ブラウザ自動操作が不安定になって**確認が止まった。**
 *   → `dist/assets/router.worker-*.js` は `self.onmessage` を使う素の ES モジュールなので、
 *     `self` を差し替えれば Node でそのまま動く。
 *
 * ★**ソースではなくビルド成果物を動かす。** 配るものと同じ物を測るため。
 * ★これは経路側の検査であって、**画面の検査ではない**（→ T-19）。
 *   「押せるか・見えるか」は必ず実機とスクリーンショットで見ること。
 *
 * 使い方（`npm run build` と `npx vite preview --port 4317` を先に）:
 *   node tools/route_probe.mjs [http://localhost:4317]
 */

import { readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const BASE = process.argv[2] ?? "http://localhost:4317";
const DIST = join(dirname(fileURLToPath(import.meta.url)), "..", "dist", "assets");

function workerPath() {
  const hit = readdirSync(DIST).find((f) => /^router\.worker-.*\.js$/.test(f));
  if (!hit) throw new Error(`${DIST} に router.worker-*.js が無い。npm run build を先に`);
  return join(DIST, hit);
}

/** ★ワーカーの外側を最小限だけ真似る。`postMessage` は id ごとに解決する */
const waiting = new Map();
globalThis.self = {
  onmessage: null,
  postMessage(m) {
    // ★途中経過（"progress"・2026-10-08〜）は結果ではない。拾うと「経路なし」と数えてしまう
    if (m?.type === "ready" || m?.type === "progress") return;
    const r = waiting.get(m.id);
    if (r) {
      waiting.delete(m.id);
      r(m);
    }
  },
};

let nextId = 1;
function ask(payload) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    waiting.set(id, resolve);
    setTimeout(() => reject(new Error(`id=${id} が応答しない`)), 180_000);
    globalThis.self.onmessage({ data: { ...payload, id } });
  });
}

const mb = (b) => `${(b / 1e6).toFixed(2)}MB`;

// ★区画と通信量は**累計**（同じ MeshGraph を使い回す＝実際の利用と同じ）。
//   1本あたりの費用が読めないので、増分も併記する。
let prevMesh = 0;
let prevBytes = 0;

function line(label, r) {
  const s = r.stats ?? {};
  const dMesh = (s.meshesTotal ?? prevMesh) - prevMesh;
  const dBytes = (s.bytesGzip ?? prevBytes) - prevBytes;
  prevMesh = s.meshesTotal ?? prevMesh;
  prevBytes = s.bytesGzip ?? prevBytes;
  const head = r.ok
    ? `所要 ${Math.round(r.seconds / 60)}分` +
      (r.baselineSeconds ? `（最速比 ${(r.seconds / r.baselineSeconds).toFixed(2)}）` : "")
    : `★断った: ${r.reason ?? ""}`;
  console.log(
    `${label.padEnd(26)} ${head}\n` +
      (s.meshesTotal === undefined
        ? `${" ".repeat(27)}（探索前に断ったので実測なし）`
        : `${" ".repeat(27)}区画 +${dMesh}（累計 ${s.meshesTotal}）/ ` +
          `通信 +${mb(dBytes)}（累計 ${mb(s.bytesGzip ?? 0)}）/ ` +
          `確定 ${(s.settled ?? 0).toLocaleString()} / ` +
          `探索 ${s.msSearch}ms ＋ 読込 ${s.msLoad}ms` +
          (s.rounds?.[0] ? ` / 直線 ${s.rounds[0].km}km / 寄せ ${s.rounds[0].snapM}m` : "")),
  );
}

/**
 * ★★**区画を二重に読み込んでいないか。**
 *
 *   統合後のノード数は「読んだ区画のノード数の合計」を**超えられない**
 *   （境界のノードが同一視されるぶん、必ず少なくなる）。
 *   2026-09-21、短距離で `ensure([c, c])` となり同じ区画を2回読んでいた。
 *   ★`verify_mesh_merge.py`（Python の写し）は区画を重複なく回すので見つけられない。
 *   **クライアント実物を動かすこの検査でしか見つからない。**
 */
function assertNoDuplicateMesh(r, index) {
  const s = r.stats;
  if (!s || s.nodeCount === undefined || !r.meshCodes) return;
  const sum = r.meshCodes.reduce((a, c) => a + index.meshes[c].nodes, 0);
  if (s.nodeCount > sum) {
    throw new Error(
      `★★区画を二重に読んでいる: nodeCount ${s.nodeCount.toLocaleString()} > ` +
        `読んだ区画の合計 ${sum.toLocaleString()}（区画 ${s.meshesTotal}枚）`,
    );
  }
  if (s.settled > s.nodeCount) {
    throw new Error(
      `★★ノードを二重に確定している: settled ${s.settled} > nodeCount ${s.nodeCount}`,
    );
  }
}

const index = await (await fetch(`${BASE}/data/index.json`)).json();
await import(workerPath());
globalThis.self.onmessage({ data: { type: "init", index, base: `${BASE}/data` } });

console.log(
  `地域 ${index.region} / 区画 ${Object.keys(index.meshes).length} 枚 / ` +
    `形式 ${index.format} / 巨大強連結成分 ` +
    `${(index.connectivity?.main_share * 100).toFixed(2)}%（除外なし）\n`,
);

const common = {
  type: "route",
  lambda: index.lambdaDefault,
  mu: index.muNarrowDefault,
  depth: index.calibratedDepthM,
  avoidUnderpass: false,
};

for (const [label, from, to, extra] of CASES()) {
  const r = await ask({ ...common, from, to, ...(extra ?? {}) });
  assertNoDuplicateMesh(r, index);
  line(label, r);
}

/**
 * ★★**並行して投げたときに区画を二重に読まないか。**
 *
 *   `self.onmessage` は `async` なので、前の仕事を待たずに次が始まる。
 *   経路の計算中に λ スライダを動かすと実際にこうなる。
 *   2026-09-21 の監査で「`ensure` の重複落としは `await` をまたげない」と分かった。
 *   ★**1本ずつ引く検査では絶対に出ない形**なので、ここで明示的に作る。
 */
{
  const p = JSON.parse(process.env.KANSUI_PROBE);
  // ★★**同じ起終点**を2本。λ を動かすと実際にこうなる。
  //   別々の経路にすると、同じ区画を**同時に**要求するとは限らず、
  //   競合が起きたり起きなかったりする＝検査が当てにならない。
  const [a, b] = p.far;
  const [c, d] = p.far;
  // ★★**まっさらな MeshGraph に戻してから投げる。**
  //   ここまでで区画は全部キャッシュ済みなので、そのまま並行させても
  //   **1枚も読み込まれず、競合が起きない**＝検査が素通りする。
  //   2026-09-21、最初そう書いてしまった（T-23 と同じ形）。
  globalThis.self.onmessage({ data: { type: "init", index, base: `${BASE}/data` } });
  // ★★**取得を遅くする。** ローカル配信は速すぎて、競合の窓が開く前に
  //   1本目の取得が終わってしまう＝**検査が素通りする**。
  //   2026-09-21、最初それに気づかず「二重読みなし」と出ていた（修正を外しても）。
  //   携帯回線では区画1枚に数百ms かかるので、そちらが実際の姿である。
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (...args) => {
    await new Promise((r) => setTimeout(r, 60));
    return realFetch(...args);
  };
  // ★await しないで2本同時に投げる（λ スライダを動かした状況の再現）
  const rs = await Promise.all([
    ask({ ...common, from: a, to: b }),
    ask({ ...common, from: c, to: d, lambda: common.lambda / 2 }), // λ を動かした想定
  ]);
  globalThis.fetch = realFetch;
  let ok = true;
  for (const r of rs) {
    try {
      assertNoDuplicateMesh(r, index);
    } catch (e) {
      ok = false;
      console.log(`  ${e.message}`);
    }
  }
  const s = rs[rs.length - 1].stats;
  console.log(
    `★並行2本（λ操作の再現）      ${ok ? "二重読みなし" : "★★二重読みあり"}
` +
      `${" ".repeat(27)}区画 ${s.meshesTotal} / ノード ${s.nodeCount.toLocaleString()} / ` +
      `確定 ${rs.map((r) => r.stats.settled.toLocaleString()).join(" と ")}`,
  );
}

/**
 * ★座標は**データから導いたもの**を使う（`tools/pick_probe_points.py` が出す）。
 *   地図を見て打った数字にすると、データを作り直すたびに意味が変わる。
 */
function CASES() {
  const p = JSON.parse(process.env.KANSUI_PROBE ?? "null");
  if (!p) throw new Error("KANSUI_PROBE（検査点の JSON）が渡されていない");
  return [
    ["近距離（つながっている）", p.near[0], p.near[1]],
    ["中距離（つながっている）", p.mid[0], p.mid[1]],
    ["長距離（つながっている）", p.far[0], p.far[1]],
    // ★近くの「つながっている」点と組にする。遠い点だと 40km の上限で先に断られ、
    //   **寄せの判定を測れない**（2026-09-21、最初そうなった）
    ["★孤立した断片が目的地", p.isolated_from, p.isolated],
    ["★海の上", p.sea_from, p.sea],
    ["★アンダーパス除外", p.mid[0], p.mid[1], { avoidUnderpass: true }],
    ["2本目（キャッシュ後）", p.near[0], p.near[1]],
  ];
}
