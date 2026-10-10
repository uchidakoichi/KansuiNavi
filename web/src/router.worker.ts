/**
 * 経路探索（Web Worker）。★A* ＋ 区画の遅延読み込み。
 *
 * ★★藤沢版との違いは2つだけ。どちらも「区画に分けたこと」から来る。
 *
 * 1. **A\* を使う**（藤沢版は素の Dijkstra）。
 *    藤沢版は市域+3km のグラフ全体がメモリに載るので探索が広くても困らなかった。
 *    全国版は**探索の広さがそのまま通信量**になる。
 *    実測（神奈川県）: 確定ノード −49〜62% / 区画はおよそ半分。
 *    ★ヒューリスティックは「目的地までの直線距離 ÷ 最高速度」。
 *      どの辺もこれより速くないので**許容的**＝**最短経路は変わらない**。
 *      前処理側の測定で、全標本の経路コストが Dijkstra と一致することを確認済み。
 *
 * 2. **必要な区画を先に読む**（F-11）。
 *    起終点の外接矩形に余裕を足した範囲の区画を読んでから探索する。
 *    ★探索が**縁**（持ち主の区画が未読み込みのノード）に届いたら、
 *      **そこで打ち切らずに範囲を広げて引き直す。**
 *      打ち切ると「区画の縁で経路が切れる」形の事故になる。
 *
 * ★コスト式は藤沢版のまま（再議論しない）:
 *     cost(e) = t(e) × (1 + λ·risk(e) + μ·narrow(e))
 *     risk(e) = max(0, S_topo − exp(−D))
 *   ★判定不能（score = 255）は risk = 0。
 *     「わからない」を「危険」に丸めない。避けたいならハード制約で除外する。
 */

import { MeshGraph, type RegionIndex } from "./meshgraph";
import { buildCoverage, type Coverage } from "./coverage";
import { meshBbox, meshCode } from "./mesh";

const STATE_UNDERPASS = 1;
const STATE_KNOWN = 2;
/** ★除外モードでアンダーパス・確定の冠水箇所にかける倍率。ほかに道があれば必ずそちらを選ぶ大きさにする */
const AVOID_PENALTY = 1000;

let graph: MeshGraph | null = null;
let coverage: Coverage | null = null;
let index: RegionIndex | null = null;
/** 配布データの置き場（init で受け取る）。読み込んだ区画を手放して作り直すときに使う */
let dataBase = "";

/** ★1回ぶんの探索の記録。何が起きたかを隠さず返す */
export interface RouteRound {
  km: number;
  /** ★起終点を道路へ寄せた距離［m］の大きいほう */
  snapM: number;
  meshes: number;
  ok: boolean;
  seconds: number;
  settled: number;
  /** ★探索中に「この先が見えていない」に当たった回数＝区画を読みに行った回数 */
  fringe: number;
}

export interface RouteRequest {
  type: "route";
  id: number;
  from: [number, number];
  to: [number, number];
  lambda: number;
  mu: number;
  depth: number;
  avoidUnderpass: boolean;
}

export interface RouteResult {
  type: "route";
  id: number;
  ok: boolean;
  reason?: string;
  nodes?: number[];
  edges?: number[];
  seconds?: number;
  baselineSeconds?: number;
  /** 地図に描く線。エッジごとに risk / state を持たせる */
  geometry?: unknown;
  /** ★F-03: 経路のノードごとの標高［m］（不明は null）。geometry の頂点と同じ順 */
  elev?: (number | null)[];
  /** ★ノードの標高が DEM10B から（形式 8。NK-07） */
  elevLow?: boolean[];
  baselineGeometry?: unknown;
  /** ★除外モードでも避けられずに通ったアンダーパス等の数 */
  unavoidable?: number;
  /** ★読み込み済みの区画の点灯区間・判定不能区間（経路の下に薄く重ねる） */
  overlay?: unknown;
  /** 読み込み済みの区画コード（★F-11 が効いていることを目で確かめるため） */
  meshCodes?: string[];
  /** ★実機で P-7 をやり直すための実測値 */
  stats?: {
    settled: number;
    meshesLoaded: number;
    meshesTotal: number;
    /** ★読み込み済みのノード総数。`settled` がこれを超えたら二重に確定している */
    nodeCount: number;
    /** 配信物の大きさ［byte］。★環境に依らない＝**これを通信量として出す** */
    bytesGzip: number;
    /** 展開後の大きさ［byte］ */
    bytesRaw: number;
    /** ★ブラウザが受け取ったバイト数。**配信側の設定で意味が変わる**（T-14） */
    bytesReceived: number;
    /** ★受け取った時点で展開済みだった区画の数。0 でなければ上の数は圧縮前 */
    meshesPreDecompressed: number;
    /** 全体［ms］（区画の取得・展開を含む） */
    ms: number;
    /** うち区画の取得・展開・作り直し［ms］ */
    msLoad: number;
    /** ★探索そのもの［ms］＝ `ms - msLoad`。**NK-06 はこちらで決める** */
    msSearch: number;
    rounds: RouteRound[];
  };
}

/** 度 → メートル（緯度に応じた概算。ヒューリスティックの下限にしか使わない） */
function metersBetween(aLon: number, aLat: number, bLon: number, bLat: number) {
  const k = Math.cos(((aLat + bLat) / 2) * (Math.PI / 180));
  const dx = (bLon - aLon) * k * 111320;
  const dy = (bLat - aLat) * 110540;
  return Math.hypot(dx, dy);
}

/** 最高速度［m/s］。★どの辺もこれより速くないこと＝ヒューリスティックが許容的であること */

/**
 * ★エッジの費用は `search()` の内側に**展開してある**（そこが唯一の定義）。
 *
 *   cost(e) = 通過時間(e) · (1 + λ·risk(e) + μ·narrow(e))
 *   risk(e) = max(0, S_topo(e) − exp(−湛水深))   ★判定不能は risk = 0
 *             （「安全だから 0」ではなく「採点しないから 0」）
 *
 * ★関数にして呼ぶと `g.edgeTime` などの getter を毎回踏み、
 *   `subarray()` の割り当てが効いてくる（→ `meshgraph.ts` の `view()`）。
 * ★`toGeoJSON` も同じ risk の式を持っている。**片方だけ直さないこと。**
 */

/**
 * ★起終点をどこまで道路に寄せてよいか［m］。
 *
 * ★★**上限が要る。** 無いと、海や山をタップしたときに
 *   **何km も離れたノードへ黙って吸着し**、まったく別の場所の経路が出る。
 *   2026-09-21 に実際に踏んだ: 直線 5km のつもりの経路が 28分・22区画になり、
 *   原因を追うのに何度も推測を外した。藤沢版も `ui.locate.max_snap_m` で
 *   同じ上限（1000m）を置いている。
 */
const MAX_SNAP_M = 1000;

/** 最も近いノードと、そこまでの距離［m］。★読み込み済みの範囲でしか探せない */
/** 配布データの形式。★`export_mesh.FORMAT_VERSION` と揃える */
const DATA_FORMAT = 8;


/**
 * 最寄りのノード。★`flag` を渡すと、**その旗が立ったノードだけ**から選ぶ。
 *
 * ★★**なぜ旗で絞るか**（2026-09-21 に実機で踏んだ）:
 *   孤立した道路の断片に寄せてしまうと、目的地へは**絶対に行けない。**
 *   A* は行けないと分かるまで探索を広げ続けるので、
 *   **地域のほぼ全域を読み込んでから**「経路が見つかりません」と出す。
 *   実測（神奈川・直線8km）: 区画 50/56 枚・16.5MB・確定ノード 1,293,073。
 *   → **つながっている道にしか寄せない。** そうすれば必ず経路がある。
 *
 * ★藤沢版 Step 5 の決定「起終点を巨大強連結成分へ寄せる」と同じ。
 *   ただし全国版は区画を遅延読み込みするので、**成分は前処理で配る。**
 *
 * ★旗はアンダーパス除外の ON/OFF で別物（`FLAG_MAIN` / `FLAG_MAIN_NO_UNDERPASS`）。
 *   チェックを入れると連結性が変わるため、**同じ旗を使い回さない。**
 */
interface Snap {
  /** 成分番号 → その成分で最も近いノードと距離［m］（1km 以内のものだけ） */
  byComp: Map<number, { node: number; m: number }>;
  /** 最も近い番号付きノードまで［m］（1km を超えていてもよい。断る理由の説明に使う） */
  nearM: number;
  /** 最も近いノードまで［m］（旗に関係なく。「近くに道路が無い」の判定） */
  anyM: number;
}

/**
 * 起終点の寄せ先の候補を集める。
 *
 * ★★寄せ先は**両端で共通する成分**の中から、寄せる距離の合計が最小のものを選ぶ（`pickComp`）。
 *   2026-09-24 のレビューで指摘: 以前は端点ごとに「1km 以内で最も大きい成分」を選んでいたので、
 *   アンダーパス除外のとき、足元（0m）の成分2の道路を捨てて 779m 先の成分1へ寄せ、
 *   両端の成分が食い違って**経路があるのに断っていた**。しかも読み込み済みの区画しだいで
 *   結果が変わった（→ 呼び出し側で 1km 以内の区画を先に読む）。
 * ★本州の小さな断片の上をタップしても、相手側が最大の成分なら 1km 以内の最大の成分へ寄る
 *   （共通の成分がそれしか無いため）。従来の「巨大強連結成分へ寄せる」と同じ振る舞い。
 */
function snapCandidates(g: MeshGraph, lon: number, lat: number, comp: Uint16Array): Snap {
  // ★T-20: getter は毎回 `subarray()` で新しいビューを作る。**ループの外で取り出す。**
  const lonA = g.lon;
  const latA = g.lat;
  const n = g.nodeCount;
  const cy = Math.cos((lat * Math.PI) / 180);
  const lim = (MAX_SNAP_M / 111320) ** 2;
  const best = new Map<number, { node: number; d: number }>();
  let nearD = Infinity;
  let anyD = Infinity;
  for (let i = 0; i < n; i++) {
    const dx = (lonA[i] - lon) * cy;
    const dy = latA[i] - lat;
    const d = dx * dx + dy * dy;
    if (d < anyD) anyD = d;
    const c = comp[i];
    if (c === 0) continue;
    if (d < nearD) nearD = d;
    if (d > lim) continue;
    const cur = best.get(c);
    if (!cur || d < cur.d) best.set(c, { node: i, d });
  }
  const toM = (d: number) => (Number.isFinite(d) ? Math.sqrt(d) * 111320 : Infinity);
  const byComp = new Map<number, { node: number; m: number }>();
  for (const [c, v] of best) byComp.set(c, { node: v.node, m: toM(v.d) });
  // ★「一番近い道路まで」と「つながっている道路まで」は**別の数字**。
  //   混ぜると「近くに道路がありません」と言いながら足元に道路がある、という
  //   嘘の説明になる（2026-09-21 に実際にそう出した）。
  return { byComp, nearM: toM(nearD), anyM: toM(anyD) };
}

/** 両端に共通する成分のうち、寄せる距離の合計が最小のもの（同じなら大きい成分）。無ければ null */
function pickComp(a: Snap, b: Snap): { comp: number; a: number; b: number; m: number } | null {
  let out: { comp: number; a: number; b: number; m: number } | null = null;
  let bestSum = Infinity;
  for (const [c, va] of a.byComp) {
    const vb = b.byComp.get(c);
    if (!vb) continue;
    const sum = va.m + vb.m;
    if (sum < bestSum || (sum === bestSum && out && c < out.comp)) {
      bestSum = sum;
      out = { comp: c, a: va.node, b: vb.node, m: Math.max(va.m, vb.m) };
    }
  }
  return out;
}

/** その点から `MAX_SNAP_M` 以内にかかる区画（★寄せる前に読む。読み込み履歴で結果を変えない） */
function meshesAround(lon: number, lat: number, level: number): string[] {
  const dLat = MAX_SNAP_M / 110540;
  const dLon = MAX_SNAP_M / (111320 * Math.cos((lat * Math.PI) / 180));
  const out = new Set<string>();
  for (const x of [lon - dLon, lon, lon + dLon]) {
    for (const y of [lat - dLat, lat, lat + dLat]) out.add(meshCode(x, y, level));
  }
  return [...out];
}

interface SearchOut {
  ok: boolean;
  nodes: number[];
  edges: number[];
  seconds: number;
  settled: number;
  /** 確定した縁のノードの数（参考。判定には使わない） */
  fringeSettled: number;
}

async function search(
  g: MeshGraph,
  src: number,
  dst: number,
  lambda: number,
  mu: number,
  threshold: number,
  avoidUnderpass: boolean,
): Promise<SearchOut> {
  const idx = index!;
  // ★配列は途中で伸びる（区画を読むとノードが増えるため）
  let n = g.nodeCount;
  let dist = new Float64Array(n).fill(Infinity);
  let prevNode = new Int32Array(n).fill(-1);
  let prevEdge = new Int32Array(n).fill(-1);
  let done = new Uint8Array(n);

  // ★★**ビューを取り出して使い回す。** `g.lon` / `g.edgeTime` などは getter で、
  //   呼ぶたびに `subarray()` が新しいビューを作る（→ `meshgraph.ts` の `view()`）。
  //   内側のループで触ると 1経路で**百万単位の割り当て**になる。
  //   2026-09-21 の実機（iPhone・直線22km）で
  //   **確定 129,524 ノードに 1,333ms**（開発機は 333,467 ノードに 530ms ＝ 6.5倍差）。
  //   ★区画を読むとバッファごと作り直されるので、**読んだら取り直す**（`refresh()`）。
  let lonA = g.lon;
  let latA = g.lat;
  let timeA = g.edgeTime;
  let scoreA = g.edgeScore;
  let stateA = g.edgeState;
  let narrowA = g.edgeNarrow;
  let head = g.csrHead;
  let adjE = g.csrEdge;
  let adjT = g.csrTo;
  const refresh = () => {
    lonA = g.lon;
    latA = g.lat;
    timeA = g.edgeTime;
    scoreA = g.edgeScore;
    stateA = g.edgeState;
    narrowA = g.edgeNarrow;
    head = g.csrHead;
    adjE = g.csrEdge;
    adjT = g.csrTo;
  };

  const grow = () => {
    const m = g.nodeCount;
    if (m === n) return;
    const d = new Float64Array(m).fill(Infinity);
    d.set(dist);
    dist = d;
    const pn = new Int32Array(m).fill(-1);
    pn.set(prevNode);
    prevNode = pn;
    const pe = new Int32Array(m).fill(-1);
    pe.set(prevEdge);
    prevEdge = pe;
    const dn = new Uint8Array(m);
    dn.set(done);
    done = dn;
    n = m;
  };

  // ★係数は先に割っておく（内側のループで毎回 1/x を計算しない）
  const invTime = 1 / idx.timeScale;
  const invScore = 1 / idx.scoreScale;
  const invNarrow = 1 / idx.narrowScale;
  const scoreUnknown = idx.scoreUnknown;
  // ★★最高速度は**配布データから受け取る**（`export_mesh` が量子化後の値を入れる）。
  //   直書きしていた 100km/h は**許容的でなかった**（2026-09-21 に実測）。
  //   量子化後の実データは 104.27km/h まであり、見積もりが過大になっていた
  //   ＝ A* が最短経路を返す保証が崩れていた。
  // ★config の routing.use_astar=false（index.json の useAstar）なら見積もり 0 ＝ Dijkstra
  const invVmax = idx.useAstar === false ? 0 : 1 / idx.vmaxMps;

  const dLon = lonA[dst];
  const dLat = latA[dst];
  /**
   * ヒューリスティック＝直線距離 ÷ 最高速度。★**許容的でなければならない**
   * （過大に見積もると最短経路でなくなる）。
   *
   * ★許容性の根拠は**係数が実際より小さいこと**:
   *   緯度1度の長さは 110,574m（赤道）〜111,694m（極）で、北緯35度では約 110,941m。
   *   ここで使う **110,540m は全域で下回る。**
   *   経度も 111,320·cos(φ) は真値（楕円体）をわずかに下回る。
   *   → 見積もりは常に **0.1〜0.4% ほど小さめ**。丸め誤差（1 ULP ≒ 10⁻¹³ 秒）は
   *     この余裕に埋もれるので、`Math.hypot` を `Math.sqrt` に替えても許容性は保たれる。
   *
   * ★`Math.hypot` は使わない。値はほぼ同じだが桁違いに遅い。
   *   ★ただし**同値ではない**（最後の桁が違うことがある）ので、
   *     ヒープの同点の崩れ方が変わり、**確定ノード数が1つ前後する**ことがある。
   *     経路そのものは変わらない（実測で所要・最速比とも一致）。
   */
  const h = (i: number) => {
    const aLat = latA[i];
    const k = Math.cos(((aLat + dLat) * 0.5) * (Math.PI / 180));
    const dx = (lonA[i] - dLon) * k * 111320;
    const dy = (aLat - dLat) * 110540;
    return Math.sqrt(dx * dx + dy * dy) * invVmax;
  };

  // 2分ヒープ（f, node）。★型付き配列で持ち、**分割代入で入れ替えない**
  //   （`[a[i], a[j]] = [a[j], a[i]]` は入れ替えのたびに配列を1つ作る）
  let hf = new Float64Array(1024);
  let hn = new Int32Array(1024);
  let hlen = 0;
  const push = (f: number, node: number) => {
    if (hlen === hf.length) {
      const nf = new Float64Array(hlen * 2);
      nf.set(hf);
      hf = nf;
      const nn = new Int32Array(hlen * 2);
      nn.set(hn);
      hn = nn;
    }
    hf[hlen] = f;
    hn[hlen] = node;
    let i = hlen++;
    while (i > 0) {
      const p = (i - 1) >> 1;
      if (hf[p] <= hf[i]) break;
      const tf = hf[p];
      hf[p] = hf[i];
      hf[i] = tf;
      const tn = hn[p];
      hn[p] = hn[i];
      hn[i] = tn;
      i = p;
    }
  };
  const pop = () => {
    const top = hn[0];
    hlen--;
    if (hlen > 0) {
      hf[0] = hf[hlen];
      hn[0] = hn[hlen];
      let i = 0;
      for (;;) {
        const l = 2 * i + 1;
        const r = l + 1;
        let m = i;
        if (l < hlen && hf[l] < hf[m]) m = l;
        if (r < hlen && hf[r] < hf[m]) m = r;
        if (m === i) break;
        const tf = hf[m];
        hf[m] = hf[i];
        hf[i] = tf;
        const tn = hn[m];
        hn[m] = hn[i];
        hn[i] = tn;
        i = m;
      }
    }
    return top;
  };

  dist[src] = 0;
  push(h(src), src);
  let settled = 0;
  let fringeSettled = 0;

  while (hlen > 0) {
    const x = pop();
    if (done[x]) continue;

    // ★★この先が見えていないノードに当たったら、**その場で持ち主の区画を読む**。
    //   読んだら同じノードをヒープへ戻して、次の回で普通に処理する。
    //   ★これが「余裕をどれだけ取るか」という見積もりを不要にする。
    //     見積もりでやろうとして 2026-09-21 に3回外した:
    //       距離の30% → 長距離で3回やり直し（経路なしが続く）
    //       距離の60% → 変わらず
    //       最低1区画 → **56区画=34MB を全部読んでしまった**
    //   探索が要求した区画だけを読めば、多すぎも少なすぎも起きない。
    if (g.isFringe(x)) {
      fringeSettled++;
      const loaded = await g.ensure([g.homeOf(x)]);
      if (loaded.length) {
        g.rebuild();
        grow();
        refresh();          // ★バッファが作り直されている。取り直さないと古い配列を見る
        push(dist[x] + h(x), x);
        continue;
      }
    }

    done[x] = 1;
    settled++;
    if (x === dst) break;
    const to = head[x + 1];
    const dx = dist[x];
    for (let k = head[x]; k < to; k++) {
      const e = adjE[k];
      // ★除外モードでは、アンダーパスと NK-09 の確定の点（known）に**非常に大きな重み**をかける（2026-10-08）。
      //   以前は通れなくしていたが、それだと出発地・目的地が**アンダーパスの向こうにしか出られない場所**のとき、
      //   起終点を数百m先の別の道路へ寄せ直してしまい、**経路の始点・終点が勝手に動いた**（大阪で 14m→419m）。
      //   重みにすれば起終点は動かず、ほかに道があれば必ずそちらを通り、無いときだけ通る（その数を画面に出す）
      const avoidHere = avoidUnderpass && (stateA[e] === STATE_UNDERPASS || stateA[e] === STATE_KNOWN);
      const y = adjT[k];
      if (done[y]) continue;
      // ★`edgeCost` をここに展開している（関数越しだと getter を毎回踏むため）。
      //   **式は `edgeCost` と1文字も変えないこと。**
      const raw = scoreA[e];
      const sc = raw === scoreUnknown ? 0 : raw * invScore;
      const risk = sc > threshold ? sc - threshold : 0;
      const nd =
        dx + timeA[e] * invTime * (1 + lambda * risk + mu * narrowA[e] * invNarrow) * (avoidHere ? AVOID_PENALTY : 1);
      if (nd < dist[y]) {
        dist[y] = nd;
        prevNode[y] = x;
        prevEdge[y] = e;
        push(nd + h(y), y);
      }
    }
  }

  if (!done[dst]) return { ok: false, nodes: [], edges: [], seconds: 0, settled, fringeSettled };

  const nodes: number[] = [dst];
  const edges: number[] = [];
  let seconds = 0;
  for (let x = dst; prevNode[x] >= 0; x = prevNode[x]) {
    edges.push(prevEdge[x]);
    seconds += timeA[prevEdge[x]] * invTime;
    nodes.push(prevNode[x]);
  }
  nodes.reverse();
  edges.reverse();
  return { ok: true, nodes, edges, seconds, settled, fringeSettled };
}

async function handleRoute(req: RouteRequest): Promise<RouteResult> {
  const t0 = performance.now();
  // ★★読み込んだ区画が溜まりすぎたら、**経路を引く前に**丸ごと手放して作り直す（2026-10-09）。
  //   区画は1つの配列に継ぎ足していくので、1枚ずつは抜けない。あちこちで経路を引くと増え続け、
  //   iPhone でページが再読み込みされた（利用者の報告）。開発機の実測では 20 本で
  //   gzip 114MB を抱え、ワーカーだけで約1GB になった。
  //   ★上限は**1本の経路で読める量（`longRoute.maxLoadMB`）の2倍**。2つの場所を行き来しても
  //   読み直さずに済む量、という自主基準（公式の数値根拠は無い）。
  //   ★手放しても区画ファイルは SW のキャッシュに残るので、作り直しで通信はほぼ増えない。
  const budget = 2 * (index!.longRoute?.maxLoadMB ?? 0) * 1e6;
  if (budget > 0 && graph!.bytesGzip > budget) graph = new MeshGraph(index!, dataBase);
  const g = graph!;
  // ★この経路で読んだぶんだけを数えるための基準（`g.msLoad` は累積）
  const loadBase = g.msLoad;
  // ★読み込みの進み具合を画面へ（外出先の遅い回線で「止まっている」と思われないため。2026-10-08）
  const bytesBase = g.bytesGzip;
  let meshesNow = 0;
  g.onProgress = (total) => {
    meshesNow++;
    (self as unknown as Worker).postMessage({
      type: "progress", id: req.id, meshes: meshesNow, mb: (total - bytesBase) / 1e6,
    });
  };
  try {
    return await handleRouteInner(req, g, t0, loadBase);
  } finally {
    g.onProgress = null;
  }
}

async function handleRouteInner(req: RouteRequest, g: MeshGraph, t0: number, loadBase: number): Promise<RouteResult> {
  const idx = index!;

  // ★★**対象範囲の外かどうかを最初に見る。**
  //   以前はここを見ず、手元の区画の中で最寄りの道を探していた。
  //   範囲の外をタップすると、**数km〜数十km先の道**が最寄りになり、
  //   「近くに道路がありません（最も近い道路まで 16707m）」と出ていた。
  //   ★利用者には「なぜ引けないのか」が分からない（2026-09-23 の報告:
  //     「神奈川圏外のルート検索ができない」）。
  //   → 配布データに**区画が無い**地点なら、はっきり「対象範囲の外」と言う。
  //   ★地図の破線は**同じ coverage.ts の結果**から描いている。
  //     「破線の外」と「範囲外」の判定が必ず一致する。
  const label = idx.regionLabel ?? idx.region;
  const outside = [
    ["出発地", req.from],
    ["目的地", req.to],
  ].filter(([, p]) => {
    const [lon, lat] = p as [number, number];
    return !coverage!.contains(lon, lat);
  });
  if (outside.length) {
    return {
      type: "route",
      id: req.id,
      ok: false,
      // ★「範囲の外」とは言い切らない。丹沢の山中のように**範囲の中でも道路データの無い
      //   一帯**が外周の外に出る（→ coverage.ts）。どちらの場合も事実である言い方にする。
      reason:
        `${outside.map(([w]) => w).join("と")}の周辺には道路データがありません` +
        `（対象: ${label}）。地図の赤い破線の内側で選んでください。` +
        "★全国版は地域ごとに順に広げています。",
    };
  }

  const km = metersBetween(req.from[0], req.from[1], req.to[0], req.to[1]) / 1000;
  const tooFar = distanceRefusal(idx, req.from, req.to, km);
  if (tooFar) return { type: "route", id: req.id, ok: false, reason: tooFar };

  const threshold = Math.exp(-req.depth);
  const rounds: RouteRound[] = [];

  // ★起終点の区画だけ読んで始める。あとは**探索が要求したぶんだけ**読む
  // ★寄せ先の候補（1km 以内）がかかる区画も先に読む。読み込み履歴で寄せ先が変わらないように
  await g.ensure([
    ...meshesAround(req.from[0], req.from[1], idx.meshLevel),
    ...meshesAround(req.to[0], req.to[1], idx.meshLevel),
  ]);
  g.rebuild();

  // ★チェックの状態で連結性が変わるので、見る成分の番号を切り替える
  // ★起終点は除外の有無に関係なく同じ道路網へ寄せる（除外で起終点が動かないように。2026-10-08）
  const comp = g.nodeComp;
  const sa = snapCandidates(g, req.from[0], req.from[1], comp);
  const sb = snapCandidates(g, req.to[0], req.to[1], comp);
  const pick = pickComp(sa, sb);
  const fail = (reason: string): RouteResult => ({
    type: "route",
    id: req.id,
    ok: false,
    reason,
    meshCodes: [...g.loaded],
    stats: statsOf(g, 0, t0, loadBase, rounds),
  });
  if (!pick) {
    // ★★断る理由は3つあり、**利用者のすべきことが違う**。混ぜない。
    //   (1) そもそも道路が無い（海の上など）              → 道路の上をタップし直す
    //   (2) 道路はあるが、1km 以内につながった道路網が無い → 少し離れた道路にする
    //   (3) どちらにも道路網はあるが、互いにつながっていない（島と本土／除外したアンダーパス）
    //   2026-09-21、(2) を (1) の文言で出してしまった（足元に道路があるのに
    //   「近くに道路がありません」と言った）。
    const bad = sa.byComp.size === 0 ? sa : sb.byComp.size === 0 ? sb : null;
    if (bad) {
      const near = Math.round(bad.anyM);
      const nearText = Number.isFinite(near) ? `（最も近い道路まで ${near}m）` : "";
      if (bad.anyM > MAX_SNAP_M) {
        return fail(`近くに道路がありません${nearText}。道路の上をタップしてください。`);
      }
      const link = Number.isFinite(bad.nearM)
        ? `。つながっている道路は ${Math.round(bad.nearM)}m 先です`
        : "";
      return fail(
        `この付近の道路は、ほかの道路網とつながっていません${nearText}${link}。` +
          "少し離れた道路の上をタップしてください。",
      );
    }
    // ★(3) 探索すると成分をまるごと読み込んでから失敗するので、**探索せずに断る**
    return fail("出発地と目的地は、道路ではつながっていません（島と本土のように道路網が分かれています）。");
  }
  const a = { node: pick.a, m: sa.byComp.get(pick.comp)!.m };
  const b = { node: pick.b, m: sb.byComp.get(pick.comp)!.m };

  const r = await search(g, a.node, b.node, req.lambda, req.mu, threshold, req.avoidUnderpass);
  rounds.push({
    km: Math.round(km),
    snapM: Math.round(Math.max(a.m, b.m)),
    meshes: g.loaded.size,
    ok: r.ok,
    seconds: Math.round(r.seconds),
    settled: r.settled,
    fringe: r.fringeSettled,
  });
  if (!r.ok) {
    return {
      type: "route",
      id: req.id,
      ok: false,
      reason: "この条件では経路が見つかりません。",
      meshCodes: [...g.loaded],
      stats: statsOf(g, r.settled, t0, loadBase, rounds),
    };
  }

  // 最速経路（λ=0・μ=0・除外なし）を併算して迂回コストを見せる
  let baseline: number | undefined;
  let baselineGeometry: unknown;
  let bl: Awaited<ReturnType<typeof search>> | undefined;
  if (req.lambda > 0 || req.mu > 0 || req.avoidUnderpass) {
    bl = await search(g, a.node, b.node, 0, 0, threshold, false);
    if (bl.ok) {
      baseline = bl.seconds;
      baselineGeometry = toGeoJSON(g, bl.nodes, bl.edges, threshold);
    }
  }
  return {
    type: "route",
    id: req.id,
    ok: true,
    nodes: r.nodes,
    edges: r.edges,
    seconds: r.seconds,
    baselineSeconds: baseline,
    geometry: toGeoJSON(g, r.nodes, r.edges, threshold),
    elev: elevOf(g, r.nodes),
    elevLow: elevLowOf(g, r.nodes),
    baselineGeometry,
    overlay: overlayGeoJSON(g, threshold, routeBox(g, [r.nodes, bl?.nodes ?? []], index!.longRoute?.marginKm ?? 2)),
    // ★除外モードでも避けられずに通ったアンダーパス・確定の冠水箇所の数（往復・連続は1か所に数える）
    unavoidable: req.avoidUnderpass ? countUnavoidable(g, r.edges) : 0,
    meshCodes: [...g.loaded],
    stats: statsOf(g, r.settled, t0, loadBase, rounds),
  };
}

/**
 * ★距離の上限（NK-06 / NK-10）。断るなら理由、受け付けるなら null。
 *   直線 `maxRouteDistanceKm`（40km）までは必ず受け付ける（今までの使い勝手を変えない）。
 *   それを超えるときは、直線 `longRoute.maxKm` まで、かつ**読む量**（起終点を囲む長方形に
 *   重なる区画の gzip の合計）が `longRoute.maxLoadMB` 以下なら受け付ける。
 *   ★道路の疎らな北海道などは遠くまで引け、密な都市部は今と同じ重さで止まる。
 */
export function distanceRefusal(
  idx: RegionIndex,
  from: [number, number],
  to: [number, number],
  km: number,
): string | null {
  if (km <= idx.maxRouteDistanceKm) return null;
  const lr = idx.longRoute;
  if (!lr) {
    return `この距離（直線 ${km.toFixed(0)}km）は対象外です。${idx.maxRouteDistanceKm}km 以内で試してください。`;
  }
  if (km > lr.maxKm) {
    return `この距離（直線 ${km.toFixed(0)}km）は対象外です。${lr.maxKm}km 以内で試してください。`;
  }
  const mb = loadMB(idx, from, to, lr.marginKm);
  if (mb > lr.maxLoadMB) {
    return (
      `この距離（直線 ${km.toFixed(0)}km）は、この地域では道路データが多すぎて対象外です` +
      `（読み込む量の見積もり ${mb.toFixed(0)}MB・上限 ${lr.maxLoadMB}MB）。` +
      `${idx.maxRouteDistanceKm}km 以内なら、どこでも引けます。`
    );
  }
  return null;
}

/** 起終点を囲む長方形（＋余裕）に重なる区画の配信サイズの合計［MB］ */
function loadMB(idx: RegionIndex, a: [number, number], b: [number, number], marginKm: number): number {
  const dLat = marginKm / 110.57;
  const dLon = marginKm / (111.32 * Math.cos((((a[1] + b[1]) / 2) * Math.PI) / 180));
  const w = Math.min(a[0], b[0]) - dLon;
  const e = Math.max(a[0], b[0]) + dLon;
  const s = Math.min(a[1], b[1]) - dLat;
  const n = Math.max(a[1], b[1]) + dLat;
  let sum = 0;
  for (const [code, m] of Object.entries(idx.meshes)) {
    const [W, S, E, N] = meshBbox(code);
    if (W < e && E > w && S < n && N > s) sum += m.gzip;
  }
  return sum / 1e6;
}

/** 経路のノードの標高が DEM10B（精度の低い標高）から来たか（形式 8。NK-07） */
function elevLowOf(g: MeshGraph, nodes: number[]): boolean[] {
  const lo = g.nodeElevLow;
  return nodes.map((n) => lo[n] === 1);
}

/** 経路のノードの標高［m］。不明は null（★0m にしない。T-09） */
function elevOf(g: MeshGraph, nodes: number[]): (number | null)[] {
  const idx = index!;
  const e = g.nodeElev;
  return nodes.map((n) => (e[n] === idx.elevUnknown ? null : e[n] / idx.elevScale));
}

/** 経路（と最速経路）のノードを囲む長方形に、余裕 `marginKm` を足したもの [西, 南, 東, 北] */
function routeBox(g: MeshGraph, paths: number[][], marginKm: number): [number, number, number, number] {
  const lonA = g.lon;
  const latA = g.lat;
  let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
  for (const p of paths)
    for (const x of p) {
      w = Math.min(w, lonA[x]); e = Math.max(e, lonA[x]);
      s = Math.min(s, latA[x]); n = Math.max(n, latA[x]);
    }
  const dLat = marginKm / 110.57;
  const dLon = marginKm / (111.32 * Math.cos((((s + n) / 2) * Math.PI) / 180));
  return [w - dLon, s - dLat, e + dLon, n + dLat];
}

/**
 * ★読み込み済みの区画の道路のうち、**点灯している区間**（雨の強さの閾値を超えた）と**判定不能の区間**を返す（2026-10-08）。
 *   経路の線だけに色を付けていたので、経路を引く前や、経路が点灯区間を避けたときに黄〜赤が1本も出なかった。
 *   ★全国は配れないので、**経路のために読んだ区画だけ**（通信は増えない）。★さらに**今の経路の周り（`box`）だけ**に絞る。
 *   ★描く数を抑えるため、risk を段に丸めて段ごとに MultiLineString にまとめる。往復の区間は1本にする。
 */
function overlayGeoJSON(g: MeshGraph, threshold: number, box: [number, number, number, number]) {
  const idx = index!;
  const lonA = g.lon;
  const latA = g.lat;
  const scoreA = g.edgeScore;
  const stateA = g.edgeState;
  const uA = g.edgeU;
  const vA = g.edgeV;
  const n = g.edgeCount;
  const STEPS = 8;
  const lit: number[][][][] = Array.from({ length: STEPS }, () => []);
  const unknown: number[][][] = [];
  const seen = new Set<number>();
  const [bw, bs, be, bn] = box;
  for (let e = 0; e < n; e++) {
    const u = uA[e];
    const v = vA[e];
    // ★今の経路の周りだけ（2026-10-09）。読み込み済み全部を返すと、あちこちで引いたあとは
    //   1本ごとに 74MB の GeoJSON を地図へ渡していた（開発機の実測・20本目）
    if (lonA[u] < bw || lonA[u] > be || latA[u] < bs || latA[u] > bn) continue;
    const key = u < v ? u * 4294967296 + v : v * 4294967296 + u; // 往復を1本に
    const raw = scoreA[e];
    const scored = raw !== idx.scoreUnknown;
    const known = stateA[e] === STATE_KNOWN;
    const unk = !scored || stateA[e] === STATE_UNDERPASS;
    const risk = scored ? raw / idx.scoreScale - threshold : 0;
    if (!(unk || known || risk > 0)) continue;
    if (seen.has(key)) continue;
    seen.add(key);
    const line = [[lonA[u], latA[u]], [lonA[v], latA[v]]];
    if (unk && !known) unknown.push(line);
    else lit[Math.min(STEPS - 1, Math.floor((known ? 1 : risk) * STEPS))].push(line);
  }
  const features: unknown[] = [];
  lit.forEach((lines, i) => {
    if (lines.length)
      features.push({ type: "Feature", properties: { kind: "risk", risk: (i + 0.5) / STEPS },
        geometry: { type: "MultiLineString", coordinates: lines } });
  });
  if (unknown.length)
    features.push({ type: "Feature", properties: { kind: "unknown" },
      geometry: { type: "MultiLineString", coordinates: unknown } });
  return { type: "FeatureCollection", features };
}

/** 経路の中で、連続するアンダーパス・確定の冠水箇所を1か所として数える */
function countUnavoidable(g: MeshGraph, edges: number[]): number {
  const st = g.edgeState;
  let n = 0;
  let prev = false;
  for (const e of edges) {
    const hit = st[e] === STATE_UNDERPASS || st[e] === STATE_KNOWN;
    if (hit && !prev) n++;
    prev = hit;
  }
  return n;
}

/** 経路を GeoJSON にする。★エッジごとに risk と state を載せて色分けに使う */
function toGeoJSON(g: MeshGraph, nodes: number[], edges: number[], threshold: number) {
  const idx = index!;
  // ★T-20: ここも getter をループの外へ。1エッジにつき7回踏んでいた
  const lonA = g.lon;
  const latA = g.lat;
  const scoreA = g.edgeScore;
  const stateA = g.edgeState;
  const nameA = g.edgeName;
  const bridgeA = g.edgeBridge;
  const demLowA = g.edgeDemLow;
  const features = edges.map((e, i) => {
    const raw = scoreA[e];
    // ★判定不能は risk を出さない。0（＝最も安全）に丸めない
    const scored = raw !== idx.scoreUnknown;
    const risk = scored ? Math.max(0, raw / idx.scoreScale - threshold) : 0;
    return {
      type: "Feature",
      // ★★`scored` を必ず出す。**`state` だけでは判定不能を見分けられない。**
      //   2026-09-22 の監査で判明: 採点できなかったエッジは
      //   `score = 255`（判定不能）なのに `state = topo` のままになる。
      //   S_topo が算出できなかった区間（DEM の欠損など）がこれに当たり、
      //   神奈川県では **946本（0.083%）**あった。
      //   `state` で色を決めていたので、**risk=0 ＝ 最も安全な色**で描いていた。
      //   ★このアプリで「わからない」を「最も安全」に見せるのは、
      //     最もしてはいけない誤りである。
      properties: {
        risk,
        scored,
        state: stateA[e],
        name: g.names[nameA[e] - 1] ?? null,
        // ★edgeBridge の値: 1 橋 / 2 トンネル（2026-09-29）。どちらもプロファイルで両端を結ぶ
        bridge: bridgeA[e] === 1,
        tunnel: bridgeA[e] === 2,
        // ★DEM10B（精度の低い標高）で推定した区間（NK-07）。一覧で出し分ける
        demLow: scored && demLowA[e] === 1,
      },
      geometry: {
        type: "LineString",
        coordinates: [
          [lonA[nodes[i]], latA[nodes[i]]],
          [lonA[nodes[i + 1]], latA[nodes[i + 1]]],
        ],
      },
    };
  });
  return { type: "FeatureCollection", features };
}

/**
 * ★**時間は2つに割って返す。**
 *   A* は途中で区画を読みに行く（`isFringe` → `ensure`）ので、
 *   全体の時間には**回線の速さが混ざる**。
 *   NK-06 の性能要件は「探索そのもの」で決めないと、測るたびに値が動く。
 *
 * ★`msLoad` は**この経路で読んだぶんだけ**を見る。`g.msLoad` は累積なので、
 *   経路の開始時点との差を取る（2本目以降は区画がキャッシュ済みで 0 になる）。
 */
function statsOf(
  g: MeshGraph,
  settled: number,
  t0: number,
  loadBase: number,
  rounds: RouteRound[] = [],
) {
  const ms = Math.round(performance.now() - t0);
  const msLoad = Math.round(g.msLoad - loadBase);
  return {
    rounds,
    settled,
    meshesLoaded: g.meshesLoaded,
    meshesTotal: g.loaded.size,
    nodeCount: g.nodeCount,
    bytesGzip: g.bytesGzip,
    bytesRaw: g.bytesRaw,
    bytesReceived: g.bytesReceived,
    meshesPreDecompressed: g.meshesPreDecompressed,
    ms,
    msLoad,
    msSearch: Math.max(0, ms - msLoad),
  };
}

/**
 * ★★**ワーカーの仕事を直列に流す。**
 *
 *   `self.onmessage` は `async` なので、**前の仕事の完了を待たずに次が始まる。**
 *   経路を計算している最中に λ スライダを動かすと、`handleRoute` が2本
 *   同時に走り、**1つの `MeshGraph` を共有したまま**進む。
 *
 *   ★そのとき起きること（2026-09-21 の監査で判明）:
 *     - 同じ区画を2回読む（`ensure` の重複落としは await をまたげない）
 *     - ★**より悪い**: 片方が区画を読んでノード数が増えると、
 *       もう片方の `dist` / `done` は古い長さのまま。
 *       `done[x]` が範囲外になると `undefined`（偽）が返り、
 *       `done[x] = 1` は**黙って無視される**ので、同じノードを何度も確定し続ける。
 *
 *   → **1本ずつ流す。** 実際の利用（1経路ずつ引く）では待ち時間は増えない。
 *   ★前の仕事が失敗しても列を止めない。
 */
let chain: Promise<unknown> = Promise.resolve();
function serialize<T>(fn: () => Promise<T>): Promise<T> {
  const next = chain.then(fn, fn);
  chain = next.catch(() => undefined);
  return next;
}

self.onmessage = async (ev: MessageEvent) => {
  const msg = ev.data;
  try {
    if (msg.type === "init") {
      index = msg.index as RegionIndex;
      if (!msg.base) throw new Error("base（絶対 URL）が渡されていない");
      // ★形式が古いと `nodeFlags` が無く、**起終点の寄せが静かに元に戻る。**
      //   区画ファイルを作り直すまで動かさない（黙って劣化させない）。
      if (!(index.vmaxMps > 0)) {
        throw new Error("index.json に vmaxMps が無い。前処理をやり直して配り直すこと");
      }
      if (index.format !== DATA_FORMAT) {
        throw new Error(
          `配布データの形式が違う（index.json は ${index.format}、` +
            `このアプリは ${DATA_FORMAT}）。前処理をやり直して配り直すこと`,
        );
      }
      dataBase = msg.base;
      graph = new MeshGraph(index, dataBase);
      // ★範囲の判定は地図の破線と同じもの（coverage.ts）を使う。起動時に一度だけ作る
      coverage = buildCoverage(index);
      // 起動時は何も読まない。★経路を引いたときに要る区画だけ読む
      // ★対象範囲の外周もここで返す。メインスレッドで同じ計算をしない（全国で数百ms 止まる）
      (self as unknown as Worker).postMessage({ type: "ready", outline: coverage!.outline });
      return;
    }
    if (msg.type === "route") {
      // ★他の仕事と混ぜない（→ `serialize` の説明）
      const r = await serialize(() => handleRoute(msg as RouteRequest));
      (self as unknown as Worker).postMessage(r);
      return;
    }
    if (msg.type === "prefetch") {
      // 現在地の周りを1区画だけ先に読む（体感のため）
      // ★経路探索と混ぜない。探索中にノード数が増えると、走っている探索の
      //   配列が古い長さのままになる（→ `serialize` の説明）
      const loaded = await serialize(async () => {
        const g = graph!;
        await g.ensure([meshCode(msg.lon, msg.lat, index!.meshLevel)]);
        g.rebuild();
        return g.loaded.size;
      });
      (self as unknown as Worker).postMessage({ type: "prefetched", meshes: loaded });
      return;
    }
  } catch (err) {
    (self as unknown as Worker).postMessage({
      type: "error",
      id: msg.id,
      message: String(err instanceof Error ? err.message : err),
    });
  }
};
