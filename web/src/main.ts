/**
 * 全国版クライアント（★最小構成）。
 *
 * ★**この版に入っていないもの**（藤沢版にはある。後から足す）:
 *   （F-01 住所検索は 2026-09-28 に入れた。現在地・F-08 も入っている）
 *   F-09 オフライン（Service Worker）／ 内水・土砂の重畳表示
 *
 * ★**この版で確かめたいこと**（P-7 を実機でやり直すため）:
 *   - 区画の遅延読み込み（F-11）が本当に効くか
 *   - A* にしたときの確定ノード数・読んだ区画数・転送量
 *   - T-13（MapLibre の Worker）/ T-14（.gz と 200+HTML）が本番で出ないか
 *   → 画面に**実測値をそのまま出す**。隠さない。
 *
 * ★文言のルール（藤沢版から継承）:
 *   絶対ルール5「**安全**」と書かない ／ 絶対ルール7 出典を常時表示 ／
 *   絶対ルール8 免責に同意を取る ／ T-02 表示のない区域にもリスクがある
 */

import { loadRoadKansui } from "./roadkansui";
import { DATA_BASE, dataUrl } from "./data-base";
import { createMap, STATE, type HazardKey, type MapHandles } from "./map";
import {
  HIGHTIDE_PREFS,
  LANDLOCKED_PREFS,
  NAISUI_EXTRA,
  NAISUI_MUNIS,
  NAISUI_SOURCE_DATE,
  naisuiCovered,
  naisuiExtraFor,
} from "./hazard-coverage";
import { registerServiceWorker } from "./sw-register";
import { buildCoverage, type Coverage } from "./coverage";
import { meshBbox } from "./mesh";
import { buildHazardList, fmtLen, hazardWhat, hazardWhere, type HazardKind } from "./hazards";
import { drawProfile } from "./profile";
import { loadPlaces, type Hit } from "./search";
import type { RegionIndex } from "./meshgraph";
import type { RouteResult } from "./router.worker";
import type { Feature, FeatureCollection } from "geojson";

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;

let index: RegionIndex;
/** ★現在地（F-01）の範囲判定で初めて要るときに作る。起動時には作らない（全国で数百ms かかる） */
let coverageCache: Coverage | null = null;
const getCoverage = () => (coverageCache ??= buildCoverage(index));
/** 外周の線（ワーカーから届く）と、地図の読み込みの両方がそろったら描く */
let outline: [number, number][][] | null = null;
let mapLoaded = false;
function drawCoverage() {
  if (!outline || !mapLoaded) return;
  // ★対象範囲の外周。ワーカーの「範囲外」判定と同じ coverage.ts の結果を描く
  handles.setCoverage({
    type: "FeatureCollection",
    features: [
      {
        type: "Feature",
        properties: {},
        geometry: { type: "MultiLineString", coordinates: outline },
      },
    ],
  });
}
let handles: MapHandles;
let worker: Worker;
let from: [number, number] | null = null;
let to: [number, number] | null = null;
let reqId = 0;
/** 直近の経路の頂点（区画の枠を描くのに使う） */
let lastMeshes = new Set<string>();

// -----------------------------------------------------------------------------
// 免責（F-10）★同意を取るまで使わせない
// -----------------------------------------------------------------------------
const AGREED = "kansui-national-agreed";

function showDisclaimer(): Promise<void> {
  return new Promise((resolve) => {
    if (localStorage.getItem(AGREED) === "1") return resolve();
    $("disclaimer").hidden = false;
    // NK-15: 同意の画面はパネルを覆うので、ポリシーは画面の中に写して開く（同意の前に読めるように）
    $("privacy-link").addEventListener("click", (ev) => {
      ev.preventDefault();
      if (document.getElementById("privacy-copy")) return;
      const copy = $("privacy").cloneNode(true) as HTMLDetailsElement;
      copy.id = "privacy-copy";
      copy.open = true;
      $("agree").before(copy);
    });
    $("agree").addEventListener("click", () => {
      try {
        localStorage.setItem(AGREED, "1");
      } catch {
        /* プライベートウィンドウ等。同意は取れているので進める */
      }
      $("disclaimer").hidden = true;
      resolve();
    });
  });
}

// -----------------------------------------------------------------------------
async function boot() {
  // ★index.json を**同意画面より前**に読む（同じ配信元。外部への通信ではない）。
  //   NK-09 の免責は、データを配っているときだけ同意画面に入れるため（7回目のレビューの申し送り）
  const res = await fetch(dataUrl("index.json"));
  if (!res.ok) throw new Error(`index.json が取得できません: ${res.status}`);
  index = await res.json();
  const rkCount = (index as { roadKansuiCount?: number }).roadKansuiCount ?? 0;
  if (rkCount) for (const el of document.querySelectorAll<HTMLElement>(".nk09")) el.hidden = false;

  await showDisclaimer();

  // 地域の中心（区画の外接矩形から）
  const codes = Object.keys(index.meshes);
  const boxes = codes.map(meshBbox);
  const w = Math.min(...boxes.map((b) => b[0]));
  const s = Math.min(...boxes.map((b) => b[1]));
  const e = Math.max(...boxes.map((b) => b[2]));
  const n = Math.max(...boxes.map((b) => b[3]));

  handles = createMap("map", [(w + e) / 2, (s + n) / 2], onMapClick, onMapError);
  handles.map.on("load", () => {
    handles.fit([w, s, e, n]);
    mapLoaded = true;
    drawCoverage();
  });

  worker = new Worker(new URL("./router.worker.ts", import.meta.url), { type: "module" });
  worker.onmessage = onWorker;
  // ★★base は**絶対 URL** で渡す。ワーカーは `/assets/` に置かれるので、
  //   相対パスだと `/assets/data/...` に解決される。
  //   2026-09-21 に実際に踏んだ: `vite preview` が SPA フォールバックで
  //   **index.html を 200 で返し**、3,626バイトの HTML をバイナリとして読もうとした。
  //   ★`res.ok` では気づけない（T-14 と同じ形）。中身の大きさで落ちて気づけた。
  worker.postMessage({ type: "init", index, base: DATA_BASE });

  wireControls();
  wireHazards();
  wireLocate();
  wireSearch();
  // ホーム画面から開いているときは追加の案内を出さない（navigator.standalone は iOS Safari 独自）
  const standalone = matchMedia("(display-mode: standalone)").matches ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true;
  if (standalone) $("a2hs").hidden = true;
  // ★NK-09: 道路冠水注意箇所（データを配っているときだけ。同意画面の文言は boot の先頭で出している）
  if (rkCount) handles.map.on("load", () => void loadRoadKansui(handles.map, $("rk-list")));
  // F-09 ★地図とデータが出てから登録する（起動の邪魔をしない）
  registerServiceWorker($<HTMLButtonElement>("update"), $("swnote"));
  // ★対象の地域の一覧は長い（51 地域）。狭い画面でパネルを埋めないよう、1行の要約＋開くと一覧にする
  {
    const el = $("region");
    const mb = (Object.values(index.meshes).reduce((a, m) => a + m.gzip, 0) / 1e6).toFixed(1);
    const nreg = (index as { regions?: string[] }).regions?.length;
    el.textContent = "";
    const d = document.createElement("details");
    const sm = document.createElement("summary");
    sm.textContent = `対象: ${nreg ? `${nreg} 地域` : "全国"} ／ 区画 ${codes.length} 枚（合計 ${mb}MB）`;
    const p = document.createElement("span");
    p.textContent = `${index.regionLabel ?? index.region}`;
    d.append(sm, p);
    el.append(d);
  }
  $("status").textContent = "地図を2回タップして、出発地と目的地を選びます。";
}

/**
 * ★地図側の失敗を**画面に出す**（最初の1件だけ）。
 *
 *   2026-09-21 に実機で `glyphs: string expected, undefined found` を踏んだ。
 *   スタイルの検証に落ちると MapLibre は**何も読み込まずに戻る**ので、
 *   **地図が真っ白になるだけで例外は出ない。** console を見るまで分からなかった。
 *
 *   ★タイルの取得失敗も同じ経路で届くため、**2件目以降は出さない。**
 *     出し続けると通信が不安定なだけで画面が警告で埋まる。
 */
let mapErrorShown = false;
function onMapError(message: string) {
  if (mapErrorShown) return;
  mapErrorShown = true;
  $("maperr").innerHTML =
    `<div class="box alert"><b>地図の表示に問題があります</b>` +
    `<div>${esc(message)}</div></div>`;
}

function wireControls() {
  const lam = $<HTMLInputElement>("lambda");
  lam.max = String(index.lambdaSteps.length - 1);
  lam.value = String(index.lambdaSteps.indexOf(index.lambdaDefault));
  const mu = $<HTMLInputElement>("mu");
  mu.value = String(index.muNarrowDefault);
  const depth = $<HTMLInputElement>("depth");
  depth.value = String(index.calibratedDepthM);
  // ★★入れた値がそのまま保たれたか**確かめる**。`step` が刻まれていると
  //   ブラウザが近い目盛りへ丸め、**校正点を選べないまま黙って動く。**
  //   2026-09-22 に実際にそうなっていた（1.407 → 1.40 ＝ 表示も「中」になる）。
  if (Number(depth.value) !== index.calibratedDepthM) {
    console.warn(
      `雨の強さの既定が丸められた: ${index.calibratedDepthM} → ${depth.value}` +
        "（input#depth の step を確認すること）",
    );
  }

  for (const el of [lam, mu, depth, $<HTMLInputElement>("avoid")]) {
    el.addEventListener("change", () => {
      showParams();
      if (from && to) route();
    });
    el.addEventListener("input", showParams);
  }
  $("clear").addEventListener("click", () => {
    // ★計算中に押されたら、その結果は捨てる（あとから経路が描かれないように）
    reqId++;
    setBusy(null);
    from = to = null;
    handles.setRoute({ type: "FeatureCollection", features: [] });
    handles.setBaseline({ type: "FeatureCollection", features: [] });
    handles.setOverlay({ type: "FeatureCollection", features: [] });
    handles.setEnds({ type: "FeatureCollection", features: [] });
    handles.focusSegment(null);
    $("hazards").innerHTML = "";
    hideProfile();
    $("out").innerHTML = "";
    $("status").textContent = "地図を2回タップして、出発地と目的地を選びます。";
  });
  showParams();
}

/**
 * 浸水想定区域の重ね表示（2026-10-11）。★経路の計算には使わない。
 * ★内水は配信が一部の市町村だけ。**地図の中心の市区町村が配信対象かを出す**（空白を「安全」と読ませない。利用者の要望）。
 *   市区町村は places.json の最寄りの地名から引く（面は持っていないので、境の近くでは隣を返すことがある）。
 */
function wireHazards() {
  $("naisui-n").textContent = `${NAISUI_MUNIS.length} 市町村・${NAISUI_SOURCE_DATE}時点`;
  const cov = $("naisui-cov");
  const tcov = $("hightide-cov");
  let places: Awaited<ReturnType<typeof loadPlaces>> | null = null;
  const on = (id: string) => $<HTMLInputElement>(id).checked;
  // ★ポータルに無い市区町村の面（藤沢市）。地図がその範囲に掛かったときだけ読む・出す
  const showExtras = () => {
    const b = handles.map.getBounds();
    for (const x of NAISUI_EXTRA) {
      const hit = on("naisui") && b.getWest() < x.bbox[2] && b.getEast() > x.bbox[0] && b.getSouth() < x.bbox[3] && b.getNorth() > x.bbox[1];
      handles.setPolygonOverlay(`naisui-x-${x.muni}`, dataUrl(x.file), x.attribution, hit);
    }
  };
  /** 地図の中心の市区町村（分からなければ null）。★配信の有無を出すときにだけ使う */
  const centerMuni = (): string | null | undefined => {
    if (!places) return undefined; // まだ読み込み中
    const c = handles.map.getCenter();
    return places.nearestMuni(c.lng, c.lat, 5);
  };
  const say = (el: HTMLElement, text: string, bad = false) => {
    el.textContent = text;
    el.classList.toggle("bad", bad);
  };
  const showCoverage = () => {
    showExtras();
    $("naisui-extra").hidden = true;
    if (!on("naisui") && !on("hightide")) return;
    const wide = handles.map.getZoom() < 10;
    const muni = wide ? null : centerMuni();
    if (on("naisui")) {
      if (wide) say(cov, "地図を拡大すると、地図の中心の市区町村に内水浸水想定区域が配信されているかを表示します。");
      else if (muni === undefined) say(cov, "市区町村を調べています…");
      else if (!muni) say(cov, "地図の中心の市区町村が分かりません（近くに地名のデータがありません）。");
      else if (naisuiCovered(muni)) say(cov, `地図の中心（${muni}）の内水浸水想定区域を表示しています。`);
      else if (naisuiExtraFor(muni)) {
        const x = naisuiExtraFor(muni)!;
        say(cov,
          `地図の中心（${muni}）の内水浸水想定区域を表示しています（ハザードマップポータルには無いため、` +
            `${x.muni}の区域図から作った面です。色分けは下の「${x.muni}」の凡例）。`);
        $("naisui-extra-src").textContent = `出典: ${x.source}`;
        $("naisui-extra").hidden = false;
      } else
        say(cov,
          `地図の中心（${muni}）の内水浸水想定区域は配信されていません。` +
            "着色が無くても浸水しないという意味ではありません。市区町村のハザードマップを確認してください。", true);
    }
    if (on("hightide")) {
      const pref = muni ? muni.split(" ")[0] : null;
      if (wide) say(tcov, "地図を拡大すると、地図の中心の都道府県に高潮浸水想定区域が配信されているかを表示します。");
      else if (muni === undefined) say(tcov, "都道府県を調べています…");
      else if (!pref) say(tcov, "地図の中心の都道府県が分かりません（近くに地名のデータがありません）。");
      else if (HIGHTIDE_PREFS.includes(pref)) say(tcov, `地図の中心（${pref}）の高潮浸水想定区域を表示しています。`);
      else if (LANDLOCKED_PREFS.includes(pref)) say(tcov, `地図の中心（${pref}）は海に面していないため、高潮浸水想定区域はありません。`);
      else
        say(tcov,
          `地図の中心（${pref}）の高潮浸水想定区域は配信されていません（作成中の場合があります）。` +
            "着色が無くても浸水しないという意味ではありません。都道府県・市区町村のハザードマップを確認してください。", true);
    }
  };
  handles.map.on("moveend", showCoverage);
  // ★チェック欄 → 重ねるレイヤ。土砂災害警戒区域等は現象ごとの3枚を一緒に出す
  const groups: Record<string, HazardKey[]> = {
    naisui: ["naisui"],
    flood_l2: ["flood_l2"],
    hightide: ["hightide"],
    lsld: ["lsld_dosekiryu", "lsld_kyukeisha", "lsld_jisuberi"],
  };
  for (const [id, keys] of Object.entries(groups)) {
    const el = $<HTMLInputElement>(id);
    el.addEventListener("change", () => {
      for (const k of keys) handles.setHazard(k, el.checked);
      $(`${id}-info`).hidden = !el.checked;
      showCoverage(); // ★消すときも藤沢市の面を消す
      // ★配信の有無を出すレイヤだけ、市区町村を引くための地名データを読む
      if (!el.checked || (id !== "naisui" && id !== "hightide") || places) return;
      loadPlaces(dataUrl("places.json"))
        .then((idx) => {
          places = idx;
          showCoverage();
        })
        .catch(() => {
          const msg = "市区町村を調べるためのデータを読み込めませんでした。通信の状態を確かめてください。";
          say(cov, msg, true);
          say(tcov, msg, true);
        });
    });
  }
}

function params() {
  return {
    lambda: index.lambdaSteps[Number($<HTMLInputElement>("lambda").value)],
    mu: Number($<HTMLInputElement>("mu").value),
    depth: Number($<HTMLInputElement>("depth").value),
    avoidUnderpass: $<HTMLInputElement>("avoid").checked,
  };
}

function showParams() {
  const p = params();
  $("lambda-v").textContent = String(p.lambda);
  $("mu-v").textContent = p.mu.toFixed(1);
  // ★mm/h の目盛りにしない（校正点が1点しかなく、予報と誤読される）
  $("depth-v").textContent = p.depth >= index.calibratedDepthM ? "強" : p.depth > 0.6 ? "中" : "弱";
}

/**
 * F-01 現在地（藤沢版の規則を継承）。
 *
 * ★位置情報は端末内でのみ使う。送信も保存もしない（ルール6）。localStorage にも置かない。
 * ★範囲の外なら断る。判定は地図の赤い破線・ワーカーと**同じ coverage** を使う。
 *   道路への寄せ距離はワーカーが見る（遠くの道へ勝手に寄せない）。
 * ★★Geolocation は**安全なコンテキスト（HTTPS / localhost）でしか動かない**（T-26 と同じ）。
 *   LAN の IP で開くと使えない。黙ってボタンを消さず、理由を出す。
 */
function wireLocate() {
  const btn = $<HTMLButtonElement>("locate");
  const msg = $("locate-msg");
  const say = (text: string, bad = false) => {
    msg.textContent = text;
    msg.classList.toggle("bad", bad);
    msg.hidden = false;
  };
  if (!window.isSecureContext || !navigator.geolocation) {
    say("★現在地はこの開き方では使えません（位置情報は HTTPS で開いたときだけ使えます）。");
    return;
  }
  btn.hidden = false;
  btn.onclick = () => {
    btn.disabled = true;
    say("現在地を取得しています…");
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        btn.disabled = false;
        const { longitude: lon, latitude: lat } = pos.coords;
        if (!getCoverage().contains(lon, lat)) {
          return say(
            `現在地の周辺には道路データがありません（対象: ${index.regionLabel ?? index.region}）。`,
            true,
          );
        }
        const role = !from || (from && to) ? "出発地" : "目的地";
        handles.map.easeTo({ center: [lon, lat], zoom: Math.max(handles.map.getZoom(), 14) });
        onMapClick(lon, lat);
        say(`現在地を${role}にしました。位置情報は端末内でのみ使い、送信していません。`);
      },
      (err) => {
        btn.disabled = false;
        say(
          err.code === err.PERMISSION_DENIED
            ? "位置情報の利用が許可されていません。ブラウザの設定を確認してください。"
            : err.code === err.TIMEOUT
              ? "現在地の取得に時間がかかりすぎました。"
              : "現在地を取得できませんでした。",
          true,
        );
      },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 30000 },
    );
  };
}

/**
 * F-01 住所・交差点名の検索。★データは検索欄に触れたときに初めて読む（→ search.ts）。
 * ★選んだ地点はタップと同じ扱い（onMapClick）。範囲の外・遠い道はワーカーが断る。
 */
function wireSearch() {
  const input = $<HTMLInputElement>("search");
  const out = $("search-out");
  const url = dataUrl("places.json");
  let hits: Hit[] = [];
  const say = (html: string) => (out.innerHTML = `<p id="search-none">${html}</p>`);

  const pick = (h: Hit) => {
    input.value = "";
    out.innerHTML = "";
    hits = []; // ★古い候補を残さない（空の欄で Enter を押すと同じ地点をもう一度選んでいた）
    const role = !from || (from && to) ? "出発地" : "目的地";
    handles.map.easeTo({ center: [h.lon, h.lat], zoom: Math.max(handles.map.getZoom(), 14) });
    onMapClick(h.lon, h.lat);
    if (role === "出発地") $("status").textContent = `${h.name}（${h.muni}）を出発地にしました。目的地を選んでください。`;
  };

  const render = async () => {
    const q = input.value;
    if (!q.trim()) {
      out.innerHTML = "";
      hits = [];
      return;
    }
    let idx;
    try {
      if (!out.innerHTML) say("検索データを読み込んでいます…");
      setBusy("住所データを読み込んでいます（初回だけ・約3MB）…");
      try {
        idx = await loadPlaces(url);
      } finally {
        setBusy(null);
      }
    } catch {
      say("検索データを読み込めませんでした。通信の状態を確かめて、もう一度入力してください。");
      return;
    }
    if (input.value !== q) return; // ★読み込み中に入力が変わった。古い結果を出さない
    const r = idx.search(q);
    hits = r.hits;
    if (!hits.length) {
      say(`該当がありません。<b>番地・よみがなでは探せません</b>。漢字で、町丁目・大字・交差点名を入力してください。
        収録は OpenStreetMap にあるものだけです（${idx.size.toLocaleString()} 件）。`);
      return;
    }
    out.innerHTML =
      hits
        .map(
          (h, i) =>
            `<button role="option" data-i="${i}"><span>${esc(h.name)}<span class="mu">${esc(h.muni)}</span></span>` +
            `<span class="k">${esc(h.kind)}</span></button>`,
        )
        .join("") +
      (r.more ? `<p id="search-none">ほかにも候補があります。市区町村名や丁目まで入力して絞り込んでください。</p>` : "");
    for (const b of out.querySelectorAll<HTMLButtonElement>("button")) {
      b.onclick = () => pick(hits[Number(b.dataset.i)]);
    }
  };

  input.oninput = () => void render();
  input.onkeydown = (e) => {
    // ★日本語入力の変換を確定する Enter で候補を選ばない（Safari は確定後に keyCode 229 が来る）
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter" && input.value.trim() && hits.length) {
      e.preventDefault();
      pick(hits[0]);
    } else if (e.key === "Escape") {
      input.value = "";
      out.innerHTML = "";
      hits = [];
    }
  };
}

// ★処理中の表示（2026-10-08）。null で消す
let busy = false;
function setBusy(text: string | null) {
  busy = text !== null;
  $("busy").hidden = !busy;
  if (text !== null) $("busy-text").textContent = text;
}
/** 処理中に地図をタップされたとき。★出発地・目的地を変えず、処理中だと知らせる */
function nudgeBusy() {
  const el = $("busy");
  $("busy-text").textContent = "計算中です。終わるまでお待ちください";
  el.classList.remove("nudge");
  void el.offsetWidth; // アニメーションをやり直す
  el.classList.add("nudge");
}

function onMapClick(lon: number, lat: number) {
  if (busy) return nudgeBusy();
  if (!from || (from && to)) {
    from = [lon, lat];
    to = null;
    $("status").textContent = "目的地をタップします。";
  } else {
    to = [lon, lat];
  }
  handles.setEnds({
    type: "FeatureCollection",
    features: ([["from", from], ["to", to]] as [string, [number, number] | null][])
      .filter(([, p]) => p)
      .map(([kind, p]) => ({
        type: "Feature",
        properties: { kind },
        geometry: { type: "Point", coordinates: p as number[] },
      })) as Feature[],
  });
  if (from && to) route();
}

/** ★ワーカーの初期化に失敗したときの案内。以後の経路の要求はワーカーへ送らず、これを出し直す */
let initFailed: string | null = null;
let workerReady = false;

function route() {
  // ★初期化に失敗したワーカーへ送ると、汎用の null 参照エラーが返って案内を上書きしてしまう
  //   （2026-09-24 のレビューで指摘）
  if (initFailed) {
    $("out").innerHTML = initFailed;
    return;
  }
  const id = ++reqId;
  $("status").textContent = "経路を計算しています…";
  setBusy("経路を計算しています…");
  worker.postMessage({ type: "route", id, from, to, ...params() });
}

function onWorker(ev: MessageEvent) {
  const m = ev.data;
  if (m.type === "ready") {
    workerReady = true;
    outline = m.outline ?? null;
    drawCoverage();
    return;
  }
  if (m.type === "progress") {
    if (m.id === reqId)
      setBusy(`道路データを読み込んでいます（${m.meshes} 枚目・${Number(m.mb).toFixed(1)}MB）…`);
    return;
  }
  if (m.type === "error") {
    setBusy(null);
    $("status").textContent = "";
    // ★★アプリだけ新しく、データが以前の Service Worker のキャッシュのまま、という組み合わせ。
    //   データの形式を上げた直後に起きる（2026-09-24 に手元で実際に起きた）。
    //   新しい Service Worker が待機しているので、「新しいデータに更新」を押せば直る。
    const stale = String(m.message).includes("配布データの形式が違う");
    const html =
      `<div class="box alert"><b>${stale ? "データの更新が必要です" : "エラー"}</b><div>` +
      (stale
        ? "アプリが新しくなりましたが、端末に保存されたデータが古いままです。" +
          "画面の「新しいデータに更新」を押してください。"
        : esc(m.message)) +
      "</div></div>";
    $("out").innerHTML = html;
    // ★準備完了の前に届いたエラー＝初期化の失敗。以後の要求はワーカーへ送らない
    if (!workerReady) initFailed = html;
    if (stale) navigator.serviceWorker?.getRegistration().then((r) => r?.update()).catch(() => {});
    return;
  }
  if (m.type !== "route") return;
  const r = m as RouteResult;
  if (r.id !== reqId) return; // 古い応答は捨てる
  setBusy(null);
  $("status").textContent = "";

  if (!r.ok) {
    handles.setRoute({ type: "FeatureCollection", features: [] });
    handles.focusSegment(null);
    $("hazards").innerHTML = "";
    hideProfile();
    $("out").innerHTML = `<div class="box alert"><b>経路が見つかりません</b><div>${esc(
      r.reason ?? "",
    )}</div></div>${statsHtml(r)}`;
    return;
  }
  drawRoute(r);
  $("out").innerHTML = summaryHtml(r) + statsHtml(r);
  renderHazards(r);
  renderProfile(r);
}

/** F-03 ルート標高プロファイル。★標高が1点も無い経路では出さない */
function renderProfile(r: RouteResult) {
  const cv = $<HTMLCanvasElement>("profile");
  const note = $("profile-note");
  const fc = r.geometry as FeatureCollection | undefined;
  cv.hidden = false;
  const ok = !!fc && !!r.elev && drawProfile(cv, fc, r.elev, r.elevLow ?? []);
  cv.hidden = !ok;
  note.hidden = !ok;
  if (ok) {
    note.innerHTML =
      "標高プロファイル。<b>立体交差の下では標高データが橋の路面を測っている</b>ため、" +
      "実際の路面と食い違う区間があります（灰色の帯）。" +
      "<b>橋の上</b>は標高データが橋の下の地面を、<b>トンネルの中</b>は上の山の地表を測るため、" +
      "どちらも両端を結んだ破線で示しています。" +
      "標高データが無い区間は線を切っています（0m として描きません）。" +
      (r.elevLow?.some(Boolean)
        ? "<b>細かい点線</b>は精度の低い標高（10mメッシュ・誤差の目安 5m）の区間です。"
        : "");
  }
}

function hideProfile() {
  $("profile").hidden = true;
  $("profile-note").hidden = true;
}

const KIND_CLASS: Record<HazardKind, string> = {
  unknown: "hz-unknown", nodata: "hz-unknown", known: "hz-known", topo: "hz-topo", topolow: "hz-topo",
};

/**
 * F-08 経路上の注意地点一覧（藤沢版の規則を継承）。
 *
 * ★件数を切ったことを隠さない。maxItems で頭を切ったら「ほか N か所」を必ず出す。
 * ★選ぶのは危険な順、並べるのは経路の順。
 * ★T-02: 一覧に出ていない区間が安全という意味にならないよう、末尾に断りを置く。
 */
function renderHazards(r: RouteResult) {
  const el = $("hazards");
  handles.focusSegment(null);
  const cfg = index.hazardList;
  const fc = r.geometry as FeatureCollection | undefined;
  if (!cfg || !fc) {
    el.innerHTML = "";
    return;
  }
  const all = buildHazardList(fc, { mergeGapM: cfg.mergeGapM });
  if (!all.length) {
    el.innerHTML =
      `<h3>経路上の注意地点</h3>
       <p class="note">この雨の強さでは、経路上に該当する区間はありません。
       <b>該当が無いことは安全を意味しません。</b>
       地形データは排水管の能力を反映できず、アンダーパスも検出しきれていない可能性があります。</p>`;
    return;
  }
  const shown = all.slice(0, cfg.maxItems).sort((a, b) => a.fromM - b.fromM);
  const rows = shown
    .map((h, i) => {
      const road = h.road ? `<span class="rd">${esc(h.road)}</span>` : "";
      return `<button class="${KIND_CLASS[h.kind]}" data-i="${i}">
        <span class="wh">${esc(hazardWhere(h))}</span>${road}
        <span class="wt">${hazardWhat(h)}</span>
        <span class="mt">区間 ${fmtLen(h.toM - h.fromM)}</span>
      </button>`;
    })
    .join("");
  const rest = all.length - shown.length;
  el.innerHTML =
    `<h3>経路上の注意地点（${all.length}か所）</h3>
     <div class="hzlist">${rows}</div>` +
    (rest > 0
      ? `<p class="note">危険な順に${cfg.maxItems}か所を選び、経路の順に並べています。ほか ${rest} か所。</p>`
      : "") +
    `<p class="note"><b>ここに出ていない区間が安全という意味ではありません。</b>
     地形データは排水管の能力を反映できず、アンダーパスも検出しきれていない可能性があります。</p>` +
    // ★NK-09: 道路冠水想定箇所を通るときは、その出典と免責をこの場で示す
    (all.some((h) => h.kind === "known")
      ? `<p class="note">道路冠水注意箇所は、各道路管理者の公表資料（PDF）をもとに<b>本アプリが位置を推定</b>して
         表示しています。国・自治体の公式な情報ではありません。地図の印を押すと元の資料と確かめ方を示します。
         掲載箇所以外でも冠水が発生する可能性があります。通行の際は現地の案内や規制情報に従ってください。</p>`
      : "");
  for (const b of el.querySelectorAll<HTMLButtonElement>(".hzlist button")) {
    b.onclick = () => {
      for (const o of el.querySelectorAll(".hzlist button")) o.classList.remove("on");
      b.classList.add("on");
      handles.focusSegment(shown[Number(b.dataset.i)].lines);
    };
  }
}

function drawRoute(r: RouteResult) {
  // ワーカーから座標も返してもらう形にしていないので、ここでは区画の枠だけ更新する。
  // ★経路の線はワーカーが返した geometry を使う（下の worker 側の拡張で埋める）
  const fc = (r as unknown as { geometry?: FeatureCollection }).geometry;
  if (fc) handles.setRoute(fc);
  const base = (r as unknown as { baselineGeometry?: FeatureCollection }).baselineGeometry;
  handles.setBaseline(base ?? { type: "FeatureCollection", features: [] });
  // ★読み込み済みの区画の点灯区間・判定不能区間（経路の周りだけ）
  handles.setOverlay(((r as unknown as { overlay?: FeatureCollection }).overlay) ?? { type: "FeatureCollection", features: [] });

  if (r.stats) {
    const codes = (r as unknown as { meshCodes?: string[] }).meshCodes ?? [];
    lastMeshes = new Set(codes);
    handles.setMeshes({
      type: "FeatureCollection",
      features: [...lastMeshes].map((c) => {
        const [w, s, e, n] = meshBbox(c);
        return {
          type: "Feature",
          properties: { code: c },
          geometry: {
            type: "LineString",
            coordinates: [
              [w, s],
              [e, s],
              [e, n],
              [w, n],
              [w, s],
            ],
          },
        } as Feature;
      }),
    });
  }
}

function summaryHtml(r: RouteResult): string {
  const min = (s: number) => `${Math.round(s / 60)}分`;
  let html = `<div class="box"><b>所要 約${min(r.seconds!)}</b>`;
  // ★除外モードでも、ほかに道が無いので通るアンダーパス等（出発地・目的地のすぐ近くに多い）
  if ((r.unavoidable ?? 0) > 0) {
    html +=
      `<div class="warn">★アンダーパス・冠水想定箇所を<b>避けられない区間を ${r.unavoidable} か所</b>通ります。` +
      `ほかに道が無いためです（出発地・目的地のすぐ近くに多い）。下の「経路上の注意地点」で場所を確かめてください。</div>`;
  }
  if (r.baselineSeconds && r.baselineSeconds > 0) {
    const ratio = r.seconds! / r.baselineSeconds;
    const diff = Math.round((r.seconds! - r.baselineSeconds) / 60);
    html +=
      `<div class="sub">最速の経路より <b>+${diff}分</b>（約${ratio.toFixed(2)}倍）</div>` +
      (ratio > index.acceptableTimeRatio
        ? `<div class="warn">★迂回の影響が大きい経路です。` +
          `アンダーパスの除外を外すと短くなることがあります。</div>`
        : "");
  }
  html += "</div>";
  return html;
}

/**
 * ★P-7 を実機でやり直すための実測値。隠さずそのまま出す。
 *
 * ★★**時間を2行に割った**（2026-09-21）。A* は途中で区画を読みに行くので、
 *   1つの数字にすると**回線の速さが探索の速さに混ざる。**
 *   実機の 4km で 659ms と出たが、そのうち何割が通信かが分からなかった。
 *   **NK-06 の性能要件は「探索そのもの」で決める。**
 *
 * ★★**通信量は `bytesGzip`（配信物の大きさ）で出す。**
 *   ブラウザが受け取ったバイト数は、配信側が `Content-Encoding: gzip` を
 *   付けるかどうかで**圧縮前にも圧縮後にもなる**（T-14）。
 *   実機で 20 区画が 20.55MB と出た（配信物は全 56 区画で 16.6MB）のがこれ。
 */
function statsHtml(r: RouteResult): string {
  if (!r.stats) return "";
  const s = r.stats;
  const mb = (b: number) => `${(b / 1e6).toFixed(2)} MB`;
  return (
    `<div class="box stats"><b>実測</b><table>` +
    row("読み込んだ区画", `${s.meshesTotal} 枚`) +
    row("通信量（累計）", mb(s.bytesGzip)) +
    row("展開後（累計）", mb(s.bytesRaw)) +
    row("確定ノード", s.settled.toLocaleString()) +
    row("★探索そのもの", `${s.msSearch} ms`) +
    row("区画の取得・展開", `${s.msLoad} ms`) +
    row("合計", `${s.ms} ms`) +
    (s.rounds[0]
      ? row("直線距離", `${s.rounds[0].km} km`) +
        row("道路への寄せ", `${s.rounds[0].snapM} m`) +
        row("★途中で読んだ区画", `${s.rounds[0].fringe} 回`)
      : "") +
    `</table><div class="note">★区画はキャッシュされます。` +
    "同じ地域で続けて引くと、2本目以降はほとんど増えません。<br>" +
    // ★T-14: 受け取ったバイト数は配信側の設定で意味が変わる。どちらだったか出す
    (s.meshesPreDecompressed > 0
      ? `★この配信は <code>.gz</code> を先に展開しています（${s.meshesPreDecompressed} 枚）。` +
        `ブラウザが受け取ったのは ${mb(s.bytesReceived)} ＝展開後の大きさで、` +
        `回線を通ったのは上の「通信量」のほうです。`
      : `★受け取ったのは ${mb(s.bytesReceived)}（圧縮済みのまま）。`) +
    "</div></div>"
  );
}

const row = (k: string, v: string) => `<tr><th>${k}</th><td>${v}</td></tr>`;
const esc = (s: string) => s.replace(/[<>&]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" })[c]!);

boot().catch((err) => {
  $("status").textContent = "";
  $("out").innerHTML = `<div class="box alert"><b>起動できません</b><div>${esc(
    String(err?.message ?? err),
  )}</div></div>`;
});

export { STATE };
