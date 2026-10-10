import { meshBbox, meshCode } from "./mesh";
/**
 * 区画（2次地域区画）を必要なぶんだけ読み込んで、1つのグラフに統合する。★F-11 の中核。
 *
 * ★藤沢版は `graph.bin` 1本を起動時に読んで終わりだった。全国版は
 *   **経路探索が触る範囲の区画だけ**を、触ったときに読む。
 *
 * ★測って決めたこと（2026-09-20、神奈川県）:
 *     短 2-5km    1本目 2区画 1.2MB → 2本目 0.6MB → 3本目 0
 *     中 5-15km   1本目 4区画 2.1MB → 2本目 **0**
 *     長 15-40km  1本目14区画 7.4MB → 2本目 1.0MB
 *   区画はキャッシュされるので、同じ地域で使い続ける利用者は初回だけ払う。
 *
 * ★★**境界ノードは「鍵」で同一視する。座標では判定しない。**
 *   区画ファイルの座標は 1e-7 度の固定小数なので、丸めで一致しないことがある。
 *   鍵（OSM node ID）は float64 で正確に運べる（2^53 未満）。
 *
 * ★T-14 を継承: `.gz` に `Content-Encoding: gzip` を付ける配信がある。
 *   そのときブラウザが先に展開するので、こちらで再度展開すると壊れる。
 *   **先頭2バイトの gzip マジック (0x1f 0x8b) で判断する。**
 */

export interface MeshInfo {
  nodes: number;
  edges: number;
  boundary: number;
  foreign: number;
  bytes: number;
  gzip: number;
}

/** 区画ファイルの先頭（★形式 4。`export_mesh._with_header` と揃える） */
type Layout = Record<string, { offset: number; length: number; type: string }>;
const MESH_MAGIC = "KNM4";

/**
 * 区画ファイル = MAGIC(4) + ヘッダ長(uint32 LE) + ヘッダ JSON + 8バイト境界まで詰め物 + 本体。
 * ★本体の中の offset は本体の先頭から数える。
 * ★形式を**中身で**確かめる。配信側が HTML を返しても（T-14）ここで止まる。
 */
function parseMesh(buf: ArrayBuffer, code: string): { layout: Layout; names: string[]; start: number } {
  const u8 = new Uint8Array(buf);
  const magic = String.fromCharCode(u8[0], u8[1], u8[2], u8[3]);
  if (magic !== MESH_MAGIC) throw new Error(`区画 ${code} の形式が違う（${JSON.stringify(magic)}）`);
  const n = new DataView(buf).getUint32(4, true);
  const head = JSON.parse(new TextDecoder().decode(u8.subarray(8, 8 + n))) as {
    layout: Layout;
    names: string[];
  };
  let start = 8 + n;
  start += (8 - (start % 8)) % 8;
  return { layout: head.layout, names: head.names, start };
}

export interface RegionIndex {
  format: number;
  region: string;
  /** ★画面に出す対象範囲の名前（例「東京都（島しょ部を除く）・神奈川県」）。古い版には無い */
  regionLabel?: string;
  /** ★対象範囲（道路のある 1km 格子）。2次区画 → 100桁のビット列。古い版には無い（→ coverage.ts） */
  cover3?: Record<string, string>;
  /** 対象範囲の外周の閉じ処理の半径［格子］（→ coverage.ts） */
  coverClosing?: number;
  origin: [number, number];
  coordScale: number;
  timeScale: number;
  scoreScale: number;
  scoreUnknown: number;
  narrowScale: number;
  /** 標高の尺度（0.1m 単位なら 10）と不明の番兵（形式 6） */
  elevScale: number;
  elevUnknown: number;
  /** ★A* のヒューリスティックの分母［m/s］。配布データから来る（直書きしない） */
  vmaxMps: number;
  state: Record<string, number>;
  meshes: Record<string, MeshInfo>;
  lambdaDefault: number;
  lambdaSteps: number[];
  muNarrowDefault: number;
  calibratedDepthM: number;
  calibratedThreshold: number;
  acceptableTimeRatio: number;
  /** F-08。★古い配布データには無い（そのときは一覧を出さない） */
  hazardList?: { maxItems: number; mergeGapM: number };
  maxRouteDistanceKm: number;
  /** ★40km を超える経路の上限（NK-10）。無い（古い index）なら 40km で断る */
  longRoute?: { maxKm: number; maxLoadMB: number; marginKm: number };
  /** false なら Dijkstra（見積もり 0）。★古い版には無い＝A* */
  useAstar?: boolean;
  meshLevel: number;
}

/** 区画の縁とみなす幅［度］。★座標の分解能（1e-7 度 ≒ 1cm）より十分小さく取る */
const EDGE = 1e-9;

/** 区画の取得をやり直す回数。★無限に粘らない */
const FETCH_ATTEMPTS = 3;
/** やり直しの間隔［ms］。回数に比例して延ばす */
const FETCH_BACKOFF_MS = 200;

const TYPED: Record<string, new (b: ArrayBuffer, o: number, n: number) => ArrayBufferView> = {
  uint8: Uint8Array,
  uint16: Uint16Array,
  int16: Int16Array,
  uint32: Uint32Array,
  float64: Float64Array,
};

/** 伸ばせる型付き配列。区画を足すたびに末尾へ積む。 */
class Grow<T extends { length: number; set(a: T, o: number): void }> {
  buf: T;
  len = 0;
  private make: (n: number) => T;
  constructor(make: (n: number) => T, cap = 1024) {
    this.make = make;
    this.buf = make(cap);
  }
  private reserve(n: number) {
    if (this.len + n <= this.buf.length) return;
    let cap = this.buf.length;
    while (cap < this.len + n) cap *= 2;
    const next = this.make(cap);
    next.set(this.buf, 0);
    this.buf = next;
  }
  push(src: T) {
    this.reserve(src.length);
    this.buf.set(src, this.len);
    this.len += src.length;
  }
  /**
   * 使っているぶんだけを切り出す。
   *
   * ★★**呼ぶたびに新しいビューを作る。** `MeshGraph` の `lon` / `edgeTime` などの
   *   getter はこれを呼ぶので、**内側のループで触ってはいけない。**
   *   2026-09-21 の実機で踏んだ: 1経路あたり百万単位の割り当てになり、
   *   実機（iPhone）が開発機の **6.5倍** 遅かった主因だった。
   *   → 使う側で**一度取り出して使い回す**（`router.worker.ts` の `refresh()`）。
   *   ★区画を読むとバッファごと作り直されるので、**読んだら取り直すこと。**
   */
  view(): T {
    return (this.buf as unknown as { subarray(a: number, b: number): T }).subarray(0, this.len);
  }
}

export class MeshGraph {
  readonly index: RegionIndex;
  private base: string;

  /** 読み込み済みの区画コード */
  readonly loaded = new Set<string>();
  /** ★境界ノードの鍵 → 全体での索引。座標では突き合わせない */
  private keyToNode = new Map<number, number>();

  private lonG = new Grow<Float64Array>((n) => new Float64Array(n));
  private latG = new Grow<Float64Array>((n) => new Float64Array(n));
  /**
   * ★ノードの持ち主の区画（`codeList` の索引）。**座標から毎回引き直さない。**
   *
   * ★★2026-09-21 の実機で分かった: `isFringe()` は**確定ノードごとに**呼ばれ、
   *   その中で `meshCode()` が**文字列を組み立てて** `Set.has()` していた。
   *   直線20km の経路では 333,467 回。**探索時間のおよそ3割**がこれだった。
   *   → 読み込むときに一度だけ引いて、以後は型付き配列の添字で済ませる。
   */
  private homeG = new Grow<Int32Array>((n) => new Int32Array(n));
  /** ★巨大強連結成分の旗（bit0 = 除外なし / bit1 = アンダーパス除外）。
   *  → `kansui/connectivity.py`。**起終点をここに寄せる。** */
  private flagG = new Grow<Uint8Array>((n) => new Uint8Array(n));
  /**
   * ★強連結成分の番号（1 = 最大。0 = 小さい断片）。除外なし / アンダーパス除外。
   *   北海道・沖縄・離島は本州とつながらないので、成分ごとに番号がある（形式 5）。
   */
  private compG = new Grow<Uint16Array>((n) => new Uint16Array(n));
  private compXG = new Grow<Uint16Array>((n) => new Uint16Array(n));
  /** ★ノードの標高［0.1m］。不明は index.elevUnknown（形式 6。F-03 ルート標高プロファイル） */
  private elevG = new Grow<Int16Array>((n) => new Int16Array(n));
  /** ★橋の区間（形式 7。F-03 のプロファイルで両端を結ぶ） */
  private bridgeG = new Grow<Uint8Array>((n) => new Uint8Array(n));
  /** ★DEM10B（精度の低い標高）で推定した区間（形式 8。NK-07） */
  private demLowG = new Grow<Uint8Array>((n) => new Uint8Array(n));
  /** ★ノードの標高が DEM10B から（形式 8。NK-07。プロファイルで破線） */
  private elevLowG = new Grow<Uint8Array>((n) => new Uint8Array(n));
  private uG = new Grow<Uint32Array>((n) => new Uint32Array(n));
  private vG = new Grow<Uint32Array>((n) => new Uint32Array(n));
  private timeG = new Grow<Uint16Array>((n) => new Uint16Array(n));
  private scoreG = new Grow<Uint8Array>((n) => new Uint8Array(n));
  private stateG = new Grow<Uint8Array>((n) => new Uint8Array(n));
  private onewayG = new Grow<Uint8Array>((n) => new Uint8Array(n));
  private narrowG = new Grow<Uint8Array>((n) => new Uint8Array(n));
  /** ★道路名は区画ごとの表で届く。ここで全体の番号へ付け替える（1始まり・0 は名前なし） */
  private nameG = new Grow<Uint32Array>((n) => new Uint32Array(n));
  /** 全体の道路名の表（`edgeName` の値 − 1 で引く） */
  readonly names: string[] = [];
  private nameId = new Map<string, number>();

  // --- 区画コードの内部番号づけ（★文字列での突き合わせを内側のループから追い出す）----
  private codeList: string[] = [];
  private codeIndex = new Map<string, number>();
  /** その番号の区画が `index.meshes` にあるか（＝読みに行けるか） */
  private codeLoadable: Uint8Array = new Uint8Array(0);
  /** その番号の区画を読み終えたか */
  private codeLoaded: Uint8Array = new Uint8Array(0);

  private intern(code: string): number {
    const hit = this.codeIndex.get(code);
    if (hit !== undefined) return hit;
    const i = this.codeList.length;
    this.codeList.push(code);
    this.codeIndex.set(code, i);
    if (i >= this.codeLoadable.length) {
      const cap = Math.max(64, (i + 1) * 2);
      const a = new Uint8Array(cap);
      a.set(this.codeLoadable);
      this.codeLoadable = a;
      const b = new Uint8Array(cap);
      b.set(this.codeLoaded);
      this.codeLoaded = b;
    }
    // ★配布されていない区画（地域の外）は「読みに行けない」＝縁ではない
    this.codeLoadable[i] = this.index.meshes[code] ? 1 : 0;
    return i;
  }

  /** ★持ち主の区画。読み込み時に決めてあるので計算しない */
  homeOf(node: number): string {
    return this.codeList[this.homeG.view()[node]];
  }

  /** 隣接（CSR）。区画を足したら作り直す */
  private head: Int32Array = new Int32Array(1);
  private adjEdge: Int32Array = new Int32Array(0);
  private adjTo: Int32Array = new Int32Array(0);
  private dirty = false;

  // ---- 実測（★P-7 を実機でやり直すために要る。NK-06 の性能要件はここから書く）----
  /**
   * ★★**「回線を通ったバイト数」ではない。**
   *   配信側が `.gz` に `Content-Encoding: gzip` を付けると（`vite preview` は付ける）
   *   **ブラウザが先に展開したもの**がここに届く。付けない配信（Cloudflare Pages）
   *   では圧縮済みのまま届く。→ **同じ数字が環境によって別のものを指す。**
   *   2026-09-21、実機の表示が 20 区画で 20.55MB になり（配信物は全 56 区画で 16.6MB）
   *   気づいた。**利用者に見せる通信量としては使えない。**
   */
  bytesReceived = 0;
  /** ★受け取った時点で既に展開されていた区画の数（＝配信側が展開した） */
  meshesPreDecompressed = 0;
  /** 配信物の大きさ（index の `gzip` の合計）。★環境に依らない＝**これを通信量として出す** */
  bytesGzip = 0;
  /** ★区画を1枚受け取るたびに呼ぶ（画面に読み込みの進み具合を出すため。2026-10-08） */
  onProgress: ((bytesGzipTotal: number) => void) | null = null;
  /** 展開後の大きさ（index の `bytes` の合計）。★環境に依らない */
  bytesRaw = 0;
  /**
   * 区画の取得・展開・グラフへの取り込み・隣接の作り直しに使った時間［ms］。
   * ★**探索の時間から差し引く。** A* は途中で区画を読みに行くので、
   *   引かないと「探索の速さ」に回線の速さが混ざる（NK-06 を決められない）。
   */
  msLoad = 0;
  meshesLoaded = 0;

  /**
   * @param base ★**絶対 URL を渡すこと。**
   *   ワーカーは `/assets/` に置かれるので、相対パスだと `/assets/data/...` に
   *   解決されてしまう。2026-09-21 に実際に踏んだ（`vite preview` が SPA
   *   フォールバックで index.html を 200 で返した）。
   */
  constructor(index: RegionIndex, base: string) {
    this.index = index;
    this.base = base.replace(/\/$/, "");
  }

  get nodeCount() {
    return this.lonG.len;
  }
  get edgeCount() {
    return this.uG.len;
  }
  get lon() {
    return this.lonG.view();
  }
  get lat() {
    return this.latG.view();
  }
  get nodeFlags() {
    return this.flagG.view();
  }
  get nodeComp() {
    return this.compG.view();
  }
  get edgeBridge() {
    return this.bridgeG.view();
  }
  get edgeDemLow() {
    return this.demLowG.view();
  }
  get nodeElevLow() {
    return this.elevLowG.view();
  }
  get nodeElev() {
    return this.elevG.view();
  }
  get nodeCompX() {
    return this.compXG.view();
  }
  get edgeU() {
    return this.uG.view();
  }
  get edgeV() {
    return this.vG.view();
  }
  get edgeTime() {
    return this.timeG.view();
  }
  get edgeScore() {
    return this.scoreG.view();
  }
  get edgeState() {
    return this.stateG.view();
  }
  get edgeOneway() {
    return this.onewayG.view();
  }
  get edgeNarrow() {
    return this.narrowG.view();
  }
  get edgeName() {
    return this.nameG.view();
  }

  /**
   * そのノードの持ち主の区画がまだ読み込まれていないか（＝この先が見えていない）。
   * ★**その区画が地域に存在しない場合は縁ではない**（読みようが無いので行き止まりで正しい）。
   */
  isFringe(node: number): boolean {
    const ci = this.homeG.buf[node];
    return this.codeLoadable[ci] === 1 && this.codeLoaded[ci] === 0;
  }

  /**
   * 必要な区画を読み込む。すでに読んだものは触らない。
   *
   * ★★**同じコードが2つ来ることがある。必ず重複を落とす。**
   *   2026-09-21 に踏んだ: `handleRoute` は起点と終点の区画を
   *   `ensure([fromCode, toCode])` で渡すが、**短距離では両方が同じ区画**になる。
   *   `filter` は重複を落とさないので、その区画を**2回読み込んでいた。**
   *   → ノードとエッジが丸ごと二重になり、
   *     ・確定ノードが増えて探索が遅くなる
   *     ・`bytesGzip` / `meshesLoaded` が**二重に数えられ、実測値が水増しされる**
   *   ★例外は出ない。経路の費用は同じなので**結果も正しく見える。**
   *   ★`verify_mesh_merge.py` は Python の写しで、区画を**重複なく**回すので
   *     この誤りは見つけられない。→ `route_probe.mjs` の不変条件で見張る。
   */
  async ensure(codes: string[]): Promise<string[]> {
    const want: string[] = [];
    for (const c of codes) {
      if (this.loaded.has(c) || !this.index.meshes[c] || want.includes(c)) continue;
      want.push(c);
    }
    if (!want.length) return want;
    const t = performance.now();
    for (const code of want) await this.load(code);
    this.dirty = true;
    this.msLoad += performance.now() - t;
    return want;
  }

  /**
   * ★★**読み込み中の区画を覚えておく。** `loaded` に入るのは `load()` の**最後**なので、
   *   `await this.fetchMesh(code)` の間は `loaded.has(code)` が偽のまま。
   *   並行して同じ区画を要求されると、`ensure` の重複落としも下の砦も
   *   **両方すり抜けて丸ごと二重になる**（2026-09-21 の監査で判明）。
   *   ★コミット e7c1b4d で直したのは**同じ呼び出しの中の重複**だけだった。
   */
  private inflight = new Map<string, Promise<void>>();

  private load(code: string): Promise<void> {
    // ★二重読み込みの最後の砦。ここを通ると区画が丸ごと二重になる
    if (this.loaded.has(code)) return Promise.resolve();
    const running = this.inflight.get(code);
    if (running) return running; // ★取得中なら**その1本に相乗りする**
    const task = this.loadOnce(code).finally(() => this.inflight.delete(code));
    this.inflight.set(code, task);
    return task;
  }

  private async loadOnce(code: string) {
    const buf = await this.fetchMesh(code);
    const { layout, names, start } = parseMesh(buf, code);
    const arr = (name: string) => {
      const l = layout[name];
      if (!l) throw new Error(`区画 ${code} に ${name} が無い`);
      const Ctor = TYPED[l.type];
      if (!Ctor) throw new Error(`知らない型: ${l.type}`);
      return new Ctor(buf, start + l.offset, l.length) as never;
    };

    const nodeLon = arr("nodeLon") as Uint32Array;
    const nodeLat = arr("nodeLat") as Uint32Array;
    const nodeFlags = arr("nodeFlags") as Uint8Array;
    // ★★またぐエッジの**両端**を見る。
    //   bKey … この区画が持ち主で、他の区画からも参照されるノード
    //   fKey … 他の区画が持ち主のノード（この区画では「縁」）
    //   ★片方だけを見ると同じノードが二重に作られ、**区画の縁で経路が切れる。**
    //     2026-09-21 に実際に踏んだ（最大連結成分が 99.20% → 35.76% に落ちた）。
    const bKey = arr("bKey") as Float64Array;
    const bLocal = arr("bLocal") as Uint32Array;
    const fKey = arr("fKey") as Float64Array;
    const fLocal = arr("fLocal") as Uint32Array;
    const n = nodeLon.length;

    // ★ローカル索引 → 全体の索引。既に知っている鍵のノードは同じものへ寄せる
    const map = new Int32Array(n).fill(-1);
    for (const [keys, locals] of [
      [bKey, bLocal],
      [fKey, fLocal],
    ] as [Float64Array, Uint32Array][]) {
      for (let i = 0; i < keys.length; i++) {
        const hit = this.keyToNode.get(keys[i]);
        if (hit !== undefined) map[locals[i]] = hit;
      }
    }

    const [ox, oy] = this.index.origin;
    const s = this.index.coordScale;
    const [bw, bs, be, bn] = meshBbox(code);
    const selfIdx = this.intern(code);
    const newLon: number[] = [];
    const newLat: number[] = [];
    const newFlag: number[] = [];
    const nodeComp = arr("nodeComp") as Uint16Array;
    const nodeCompX = arr("nodeCompX") as Uint16Array;
    const newComp: number[] = [];
    const newCompX: number[] = [];
    const nodeElev = arr("nodeElev") as Int16Array;
    const newElev: number[] = [];
    const nodeElevLow = arr("nodeElevLow") as Uint8Array;
    const newElevLow: number[] = [];
    const newHome: number[] = [];
    let next = this.nodeCount;
    for (let i = 0; i < n; i++) {
      if (map[i] >= 0) continue;
      map[i] = next++;
      const nlon = nodeLon[i] / s + ox;
      const nlat = nodeLat[i] / s + oy;
      newLon.push(nlon);
      newLat.push(nlat);
      // ★同じノードは同じ旗（前処理が地域全体で一度に決めている）。
      //   既に知っているノードは触らない ＝ 上の `continue` でよい。
      newFlag.push(nodeFlags[i]);
      newComp.push(nodeComp[i]);
      newCompX.push(nodeCompX[i]);
      newElev.push(nodeElev[i]);
      newElevLow.push(nodeElevLow[i]);
      // ★持ち主の区画はここで一度だけ引く（→ `homeG` のコメント）。
      //   ★ほとんどのノードは**いま読んでいる区画**の中にあるので、まず矩形で判定する。
      //     文字列を作るのは境界をまたいだノードだけ（1区画あたり数百件）。
      //   ★縁ちょうどは `meshCode` に任せる（矩形は度、`meshCode` は秒の整数で
      //     判定しており、丸めが一致しない可能性があるため）。
      //   ★余白 1e-9 度は座標の刻み（1e-7 度 ≒ 1cm）より 100倍細かい。
      //     しかも区画の縁（緯度 0.08333…度）は 1e-7 の倍数で表せないので、
      //     この余白に入ること自体まず無い。入ったら上の `meshCode` が答える。
      newHome.push(
        nlon > bw + EDGE && nlon < be - EDGE && nlat > bs + EDGE && nlat < bn - EDGE
          ? selfIdx
          : this.intern(meshCode(nlon, nlat, this.index.meshLevel)),
      );
    }
    this.lonG.push(Float64Array.from(newLon));
    this.latG.push(Float64Array.from(newLat));
    this.flagG.push(Uint8Array.from(newFlag));
    this.compG.push(Uint16Array.from(newComp));
    this.compXG.push(Uint16Array.from(newCompX));
    this.elevG.push(Int16Array.from(newElev));
    this.elevLowG.push(Uint8Array.from(newElevLow));
    this.homeG.push(Int32Array.from(newHome));

    // ★鍵を覚えて、次の区画で同一視できるようにする（両方のリストを入れる）
    for (const [keys, locals] of [
      [bKey, bLocal],
      [fKey, fLocal],
    ] as [Float64Array, Uint32Array][]) {
      for (let i = 0; i < keys.length; i++) {
        if (!this.keyToNode.has(keys[i])) this.keyToNode.set(keys[i], map[locals[i]]);
      }
    }

    const eu = arr("edgeU") as Uint32Array;
    const ev = arr("edgeV") as Uint32Array;
    const m = eu.length;
    const gu = new Uint32Array(m);
    const gv = new Uint32Array(m);
    for (let i = 0; i < m; i++) {
      gu[i] = map[eu[i]];
      gv[i] = map[ev[i]];
    }
    this.uG.push(gu);
    this.vG.push(gv);
    this.timeG.push(arr("edgeTime") as Uint16Array);
    this.scoreG.push(arr("edgeScore") as Uint8Array);
    this.stateG.push(arr("edgeState") as Uint8Array);
    this.onewayG.push(arr("edgeOneway") as Uint8Array);
    this.narrowG.push(arr("edgeNarrow") as Uint8Array);
    this.bridgeG.push(arr("edgeBridge") as Uint8Array);
    this.demLowG.push(arr("edgeDemLow") as Uint8Array);
    // ★区画の表の番号 → 全体の番号
    const local = arr("edgeName") as Uint16Array;
    const toGlobal = new Uint32Array(names.length + 1);
    for (let i = 0; i < names.length; i++) {
      let id = this.nameId.get(names[i]);
      if (id === undefined) {
        this.names.push(names[i]);
        id = this.names.length;
        this.nameId.set(names[i], id);
      }
      toGlobal[i + 1] = id;
    }
    const gname = new Uint32Array(local.length);
    for (let i = 0; i < local.length; i++) gname[i] = toGlobal[local[i]];
    this.nameG.push(gname);

    this.loaded.add(code);
    this.codeLoaded[this.intern(code)] = 1;
    this.meshesLoaded++;
  }

  /**
   * 区画を1つ取得して、必要なら展開する。
   *
   * ★★**一度の失敗で経路を落とさない。**
   *   2026-09-21、100本引くうちの1本が `fetch failed` で丸ごと落ちた。
   *   ★このアプリは**豪雨のさなかに携帯回線で使われる**ことを前提にしている。
   *     取りこぼしは例外ではなく、起きるものとして扱う。
   *   ★ただし**無限に粘らない**。数えた回数で諦め、**人に読める文で**返す。
   *     `fetch failed` をそのまま画面に出さない。
   *
   * ★数える（`bytesReceived` など）のは**成功したときだけ**。
   *   やり直しのたびに足すと実測値が水増しされる。
   */
  private async fetchMesh(code: string): Promise<ArrayBuffer> {
    const info = this.index.meshes[code];
    let last: unknown;
    for (let attempt = 0; attempt < FETCH_ATTEMPTS; attempt++) {
      if (attempt) await new Promise((r) => setTimeout(r, FETCH_BACKOFF_MS * attempt));
      try {
        return await this.fetchMeshOnce(code, info);
      } catch (e) {
        last = e;
      }
    }
    throw new Error(
      `区画 ${code} を取得できませんでした（${FETCH_ATTEMPTS}回試しました）。` +
        `通信の状態を確かめて、もう一度お試しください。` +
        `［${last instanceof Error ? last.message : String(last)}］`,
    );
  }

  private async fetchMeshOnce(code: string, info: MeshInfo): Promise<ArrayBuffer> {
    const res = await fetch(`${this.base}/mesh/${code}.bin.gz`);
    if (!res.ok) throw new Error(`区画 ${code} が取得できない: ${res.status}`);
    const raw = await res.arrayBuffer();

    // ★T-14: 配信側が Content-Encoding: gzip を付けるとブラウザが先に展開する。
    //   ヘッダは環境によって見えないので、**先頭2バイトのマジックで判断する。**
    const head = new Uint8Array(raw, 0, Math.min(2, raw.byteLength));
    const preDecompressed = head[0] !== 0x1f || head[1] !== 0x8b;
    let out: ArrayBuffer;
    if (preDecompressed) {
      if (raw.byteLength < info.bytes) {
        // ★T-14: 存在しないパスに 200 + index.html を返す配信がある。
        //   res.ok では気づけない。**中身の大きさで判定する。**
        throw new Error(
          `区画 ${code} の中身が小さすぎる（${raw.byteLength} < ${info.bytes}）。` +
            `配信漏れか、HTML が返っている可能性がある`,
        );
      }
      out = raw;
    } else {
      const ds = new DecompressionStream("gzip");
      out = await new Response(new Blob([raw]).stream().pipeThrough(ds)).arrayBuffer();
      if (out.byteLength !== info.bytes) {
        throw new Error(`区画 ${code} の大きさが index と違う（${out.byteLength} != ${info.bytes}）`);
      }
    }

    // ★成功したときだけ数える（やり直しで水増ししないため）
    this.bytesReceived += raw.byteLength;
    this.bytesGzip += info.gzip;
    this.bytesRaw += info.bytes;
    if (preDecompressed) this.meshesPreDecompressed++;
    this.onProgress?.(this.bytesGzip);
    return out;
  }

  /** 隣接を作り直す。★一方通行でない辺は逆向きも張る。 */
  rebuild() {
    if (!this.dirty) return;
    const tRebuild = performance.now();
    const n = this.nodeCount;
    const eu = this.edgeU;
    const ev = this.edgeV;
    const ow = this.edgeOneway;
    const m = eu.length;

    let deg = 0;
    const count = new Int32Array(n + 1);
    for (let i = 0; i < m; i++) {
      count[eu[i]]++;
      deg++;
      if (!ow[i]) {
        count[ev[i]]++;
        deg++;
      }
    }
    const head = new Int32Array(n + 1);
    for (let i = 0; i < n; i++) head[i + 1] = head[i] + count[i];
    const cur = head.slice(0, n);
    const adjEdge = new Int32Array(deg);
    const adjTo = new Int32Array(deg);
    for (let i = 0; i < m; i++) {
      let k = cur[eu[i]]++;
      adjEdge[k] = i;
      adjTo[k] = ev[i];
      if (!ow[i]) {
        k = cur[ev[i]]++;
        adjEdge[k] = i;
        adjTo[k] = eu[i];
      }
    }
    this.head = head;
    this.adjEdge = adjEdge;
    this.adjTo = adjTo;
    this.dirty = false;
    this.msLoad += performance.now() - tRebuild;
  }

  // ★★CSR をそのまま渡す。**内側のループでは `neighbors()` を使わない。**
  //   `neighbors()` は確定ノードごとにオブジェクトを1つ作るので、
  //   1経路で十万単位の割り当てになる（実機が遅かった原因のひとつ）。
  get csrHead() {
    return this.head;
  }
  get csrEdge() {
    return this.adjEdge;
  }
  get csrTo() {
    return this.adjTo;
  }

  neighbors(node: number): { from: number; to: number; edge: Int32Array; dst: Int32Array } {
    return {
      from: this.head[node],
      to: this.head[node + 1],
      edge: this.adjEdge,
      dst: this.adjTo,
    };
  }
}
