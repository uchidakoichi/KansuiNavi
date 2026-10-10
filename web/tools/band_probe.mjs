/**
 * ★距離帯ごとに多数の経路を引き、**確定ノード数の分布**を測る。NK-06 の土台。
 *
 * なぜ確定ノード数か（2026-09-21）:
 *   性能要件は実機の時間で書きたいが、実機で測れるのは数本しかない。
 *   一方 **確定ノード数は端末に依らない**（同じ起終点なら同じ）。
 *   → ここで帯ごとに多数集め、**実機で測った「1ノードあたりの時間」**と掛ける。
 *
 * ★掛け算で出した値は**実測ではない**。報告では必ずそう書くこと。
 *   実機で直接測ったのは 21km（224,026ノード / 1,167ms）と
 *   35km（546,609ノード / 2,963ms）の2本だけ。
 *
 * 使い方:
 *   uv run python tools/pick_probe_points.py --bands 25 > /tmp/probe.json   (preprocess で)
 *   KANSUI_PROBE="$(head -1 /tmp/probe.json)" node tools/band_probe.mjs
 */

import { readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const BASE = process.argv[2] ?? "http://localhost:4317";
const DIST = join(dirname(fileURLToPath(import.meta.url)), "..", "dist", "assets");

/**
 * ★実機で測った1ノードあたりの時間［µs］。**掛け算の根拠はこれだけ**。
 *
 * ★2026-09-21、区画の二重読み込みを直した版で測り直した。
 *   確定ノード数は 18〜27% 減ったが、**1ノードあたりの時間は変わらない**
 *   （5.2〜6.1µs）＝ 直したのは「探索の量」であって「1歩の速さ」ではない。
 */
const DEVICE_US_PER_NODE = [
  { km: 21, nodes: 183_893, ms: 1112, note: "修正後・区画は全てキャッシュ済み" },
  { km: 35, nodes: 398_318, ms: 2068, note: "修正後" },
];

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

const q = (a, p) => {
  const s = [...a].sort((x, y) => x - y);
  const i = Math.min(s.length - 1, Math.max(0, Math.round((s.length - 1) * p)));
  return s[i];
};

const index = await (await fetch(`${BASE}/data/index.json`)).json();
const worker = readdirSync(DIST).find((f) => /^router\.worker-.*\.js$/.test(f));
await import(join(DIST, worker));
globalThis.self.onmessage({ data: { type: "init", index, base: `${BASE}/data` } });

const probe = JSON.parse(process.env.KANSUI_PROBE ?? "null");
if (!probe?.bands) throw new Error("KANSUI_PROBE に bands が無い（--bands N を付けて作る）");

const usPerNode =
  DEVICE_US_PER_NODE.reduce((a, d) => a + (d.ms * 1000) / d.nodes, 0) /
  DEVICE_US_PER_NODE.length;

console.log(
  `地域 ${index.region} / 形式 ${index.format} / vmax ${(index.vmaxMps * 3.6).toFixed(2)}km/h\n` +
    `★実機の1ノードあたり: ` +
    DEVICE_US_PER_NODE.map((d) => `${((d.ms * 1000) / d.nodes).toFixed(2)}µs(${d.km}km)`).join(
      " / ",
    ) +
    ` → 平均 ${usPerNode.toFixed(2)}µs\n`,
);
console.log(
  "帯".padEnd(12) +
    "n".padStart(4) +
    "確定ノード 中央".padStart(16) +
    "p90".padStart(11) +
    "最大".padStart(11) +
    "  開発機 探索 p90" +
    "  ★実機 換算 p90",
);

const common = {
  type: "route",
  lambda: index.lambdaDefault,
  mu: index.muNarrowDefault,
  depth: index.calibratedDepthM,
  avoidUnderpass: false,
};

const rows = [];
for (const band of probe.bands) {
  const nodes = [];
  const ms = [];
  let failed = 0;
  for (const [from, to] of band.pairs) {
    // ★1本ごとにまっさらな MeshGraph に戻す（2026-10-09）。全国の標本は地点が散らばるので、
    //   使い回すと読んだ区画が溜まり続け、Node が 3.5GB でメモリ不足で落ちた
    globalThis.self.onmessage({ data: { type: "init", index, base: `${BASE}/data` } });
    const r = await ask({ ...common, from, to });
    if (!r.ok || !r.stats) {
      failed++;
      // ★「経路なし」を数だけで済ませない。**理由が違えば意味が違う**
      //   （40km の上限で断った / つながっていない / 本当に見つからない）
      // ★「経路なし」と「例外」は別物。混ぜると原因を取り違える
      //   （2026-09-21、`fetch failed` を「経路なし」と数えていた）
      console.log(
        r.type === "error"
          ? `  ★★例外: ${r.message}`
          : `  ★経路なし: ${r.reason ?? "(理由なし)"}`,
      );
      continue;
    }
    nodes.push(r.stats.settled);
    ms.push(r.stats.msSearch);
    // ★不変条件（→ `route_probe.mjs` の `assertNoDuplicateMesh`）
    const sum = (r.meshCodes ?? []).reduce((a, c) => a + index.meshes[c].nodes, 0);
    if (r.stats.nodeCount > sum || r.stats.settled > r.stats.nodeCount) {
      throw new Error(
        `★★区画を二重に読んでいる: nodeCount ${r.stats.nodeCount.toLocaleString()} ` +
          `/ 合計 ${sum.toLocaleString()} / settled ${r.stats.settled.toLocaleString()}`,
      );
    }
  }
  const p90n = q(nodes, 0.9);
  const row = {
    band: `${band.lo}–${band.hi}km`,
    n: nodes.length,
    failed,
    median: q(nodes, 0.5),
    p90: p90n,
    max: q(nodes, 1),
    devP90: q(ms, 0.9),
    deviceP90Ms: Math.round((p90n * usPerNode) / 1000),
  };
  rows.push(row);
  console.log(
    row.band.padEnd(12) +
      String(row.n).padStart(4) +
      row.median.toLocaleString().padStart(16) +
      row.p90.toLocaleString().padStart(11) +
      row.max.toLocaleString().padStart(11) +
      `${row.devP90}ms`.padStart(16) +
      `${(row.deviceP90Ms / 1000).toFixed(1)}秒`.padStart(16) +
      (row.failed ? `  ★経路なし ${row.failed}` : ""),
  );
}

console.log(
  "\n★「実機 換算」は**実測ではない**。確定ノード数（端末に依らない）に、" +
    `実機で測った ${usPerNode.toFixed(2)}µs/ノード を掛けたもの。`,
);
console.log("★区画の取得・展開の時間は含まない（回線しだいなので別に扱う）。");
