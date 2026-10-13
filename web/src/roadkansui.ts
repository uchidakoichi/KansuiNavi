/**
 * NK-09 道路冠水注意箇所の表示（2026-10-02）。
 *
 * ★座標付きの公式配布は無い。各道路管理者の PDF から**本アプリが位置を推定**した
 *   （preprocess/tools/nk09_pilot/）。段ごとに見せ方を変える（利用者判断 2026-10-02）:
 *   confirmed … 点。経路計算にも効いている（区間を最上位のリスクに）
 *   uncertain … おおよその円。経路計算には効かせていない
 *   unlocated … 地図に出さず、パネルの一覧だけ
 * ★どの段でも、押すと**元の資料（PDF の該当ページ）と、どこを見れば確かめられるか**を出す。
 *   確かめる人が運営に1人しかいないので、利用者が自分で目視できるよう案内する（利用者判断 2026-10-02）。
 * ★リンク先は国の資料。押すのは利用者で、アプリから位置などを送ることはない（NK-15 の範囲内）。
 */

import { Popup, type Map as MLMap } from "maplibre-gl";
import type { Feature, FeatureCollection, Polygon } from "geojson";
import { ROAD_KANSUI_ATTRIB } from "./map";
import { dataUrl } from "./data-base";

interface Src { title: string; publisher: string; date: string; url: string; page: number; hint: string }
interface Item {
  id: string; tier: "confirmed" | "uncertain" | "unlocated";
  lon?: number; lat?: number; radiusM?: number;
  route: string; place: string; manager: string; method: string; src: Src;
}
interface Doc { attribution: string; processing: string; items: Item[] }

const COLOR = "#8b1a4a";

function esc(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

function circle(lon: number, lat: number, r: number): Polygon {
  const ring: [number, number][] = [];
  const k = 111320 * Math.cos((lat * Math.PI) / 180);
  for (let i = 0; i <= 48; i++) {
    const a = (i / 48) * 2 * Math.PI;
    ring.push([lon + (r * Math.cos(a)) / k, lat + (r * Math.sin(a)) / 110570]);
  }
  return { type: "Polygon", coordinates: [ring] };
}

/** 押したときの説明。★確定でも「取り違えの可能性」を書く。「安全」とは書かない（ルール5） */
/** 円の半径の表示（1km 以上は km） */
function fmtRadius(m: number): string {
  return m >= 1000 ? `${+(m / 1000).toFixed(1)}km` : `${m}m`;
}

function describe(it: Item): string {
  // ★位置の出所で書き分ける: 整備局の公表データ（Webマップ）か、資料の地図から本アプリが推定したものか
  const official = it.method.startsWith("整備局の");
  const head =
    it.tier === "confirmed"
      ? official
        ? "<b>冠水想定箇所</b>（位置は整備局の公表データ）"
        : "<b>道路冠水注意箇所</b>（位置は推定・2つの手がかりが一致）"
      : it.tier === "uncertain"
        ? `<b>道路冠水注意箇所（位置は未確認）</b><br>この円（半径 約${fmtRadius(it.radiusM!)}）のどこかにあると推定しています。経路の計算には使っていません。`
        : "<b>道路冠水注意箇所（位置を決められませんでした）</b>";
  const caveat =
    it.tier === "confirmed"
      ? official
        ? "<br><span class='note'>公表された位置が、説明表（PDF）に書かれた市区町村の中にあり、道路に結び付けられたものです。経路の計算では、この区間を最もリスクの高い区間として扱っています。</span>"
        : "<br><span class='note'>資料の地図と道路データの位置が一致したものです。近くの別の立体交差と取り違えている可能性は残ります。経路の計算では、この区間を最もリスクの高い区間として扱っています。</span>"
      : "";
  return (
    `<div class="rkpop">${head}${caveat}` +
    `<p>${esc(it.route)}<br>${esc(it.place)}${it.manager ? `<br>${esc(it.manager)}` : ""}</p>` +
    // ★説明表へのリンクが無い点（公表データでリンクが別の箇所と重複していたもの）は、理由だけを示す
    (it.src.url
      ? `<p class="note"><b>確かめ方</b>: 元の資料の <b>${esc(it.src.hint)}</b> と、この地図の位置を見比べてください。<br>` +
        `<a href="${encodeURI(it.src.url)}#page=${it.src.page}" target="_blank" rel="noopener noreferrer">` +
        `${esc(it.src.title)}（${esc(it.src.date)}・PDF ${it.src.page}ページ）</a><br>`
      : `<p class="note">${esc(it.src.hint)}。<br>`) +
    `${esc(it.src.publisher)}</p>` +
    `<p class="note">位置の出所: ${esc(it.method)}。${official ? "公表データを本アプリが加工して表示" : "公表資料をもとに本アプリが位置を推定"}したもので、国・自治体の公式な情報ではありません。</p></div>`
  );
}

export async function loadRoadKansui(map: MLMap, listEl: HTMLElement): Promise<void> {
  // ★読めなかったら**画面に出す**。黙って返すと、印も一覧も出ないのに誰も気づかない
  //   （2026-10-03、JSON に NaN が混じって読めず、そのまま何も表示されていなかった）
  const fail = (why: string) => {
    listEl.innerHTML = `<summary>冠水想定箇所</summary><p class="note">冠水想定箇所のデータを読み込めませんでした（${esc(why)}）。表示していません。</p>`;
    listEl.hidden = false;
  };
  const res = await fetch(dataUrl("road_kansui.json"));
  if (!res.ok) return fail(`HTTP ${res.status}`);
  let doc: Doc;
  try {
    doc = (await res.json()) as Doc; // ★無いパスに 200 ＋ HTML が返る配信がある（T-14）。中身で判断する
  } catch {
    return fail("形式が正しくありません");
  }
  if (!Array.isArray(doc?.items)) return fail("形式が正しくありません");
  const byId = new Map(doc.items.map((it) => [it.id, it]));

  const pts: FeatureCollection = {
    type: "FeatureCollection",
    features: doc.items
      .filter((it) => it.tier === "confirmed")
      .map((it) => ({ type: "Feature", properties: { id: it.id }, geometry: { type: "Point", coordinates: [it.lon!, it.lat!] } })),
  };
  const circles: FeatureCollection = {
    type: "FeatureCollection",
    features: doc.items
      .filter((it) => it.tier === "uncertain")
      .map((it): Feature => ({ type: "Feature", properties: { id: it.id }, geometry: circle(it.lon!, it.lat!, it.radiusM!) })),
  };
  // ★要確認の円の中心の印。説明を開くのはこの印だけ（円の面を押せるようにすると、半径 400m（いまは 1km）の中に
  //   出発地・目的地を置けなくなっていた。2026-10-08 利用者の指摘）
  const centers: FeatureCollection = {
    type: "FeatureCollection",
    features: doc.items
      .filter((it) => it.tier === "uncertain")
      .map((it) => ({ type: "Feature", properties: { id: it.id }, geometry: { type: "Point", coordinates: [it.lon!, it.lat!] } })),
  };
  map.addSource("rk-points", { type: "geojson", data: pts, attribution: ROAD_KANSUI_ATTRIB });
  map.addSource("rk-centers", { type: "geojson", data: centers });
  map.addSource("rk-circles", { type: "geojson", data: circles, attribution: ROAD_KANSUI_ATTRIB });
  map.addLayer({ id: "rk-uncertain-fill", type: "fill", source: "rk-circles",
    paint: { "fill-color": COLOR, "fill-opacity": 0.08 } });
  // ★色だけで区別しない: 未確認は破線の円、確定は白縁の点
  map.addLayer({ id: "rk-uncertain-line", type: "line", source: "rk-circles",
    paint: { "line-color": COLOR, "line-width": 1.5, "line-dasharray": [2, 2] } });
  // 中心の印は白抜きの丸（確定の点＝塗りつぶしと形で区別する）
  map.addLayer({ id: "rk-uncertain-center", type: "circle", source: "rk-centers",
    paint: { "circle-radius": 7, "circle-color": "#ffffff", "circle-stroke-color": COLOR, "circle-stroke-width": 2.5 } });
  map.addLayer({ id: "rk-confirmed", type: "circle", source: "rk-points",
    paint: { "circle-radius": 6, "circle-color": COLOR, "circle-stroke-color": "#fff", "circle-stroke-width": 1.5 } });
  // ★押せる範囲だけを広げる（見た目は小さいまま）。直径 44px ＝ iPhone で押しやすい最小の大きさ。
  //   透明な丸を重ね、これを押す対象にする（2026-10-08 利用者の指摘「白い丸が押しにくい」）
  //   ★不透明度は 0（描かない）。0.01 にしたら全国表示で約4,700個が重なり黒い塊になった（2026-10-09）。
  //   MapLibre は不透明度 0 の丸を描かないが、当たり判定からは外さない
  for (const [id, src] of [["rk-hit-uncertain", "rk-centers"], ["rk-hit-confirmed", "rk-points"]] as const) {
    map.addLayer({ id, type: "circle", source: src, paint: { "circle-radius": 22, "circle-opacity": 0 } });
  }

  for (const id of ["rk-hit-confirmed", "rk-hit-uncertain"]) {
    map.on("click", id, (e) => {
      const f = e.features?.[0];
      const it = f && byId.get(String(f.properties?.id));
      if (!it) return;
      new Popup({ maxWidth: "320px" }).setLngLat(e.lngLat).setHTML(describe(it)).addTo(map);
    });
    map.on("mouseenter", id, () => (map.getCanvas().style.cursor = "pointer"));
    map.on("mouseleave", id, () => (map.getCanvas().style.cursor = ""));
  }

  // ★位置を決められなかったもの・未確認のものはパネルに一覧で出す（地図に無いものを黙って落とさない）
  const un = doc.items.filter((it) => it.tier === "unlocated");
  const unc = doc.items.filter((it) => it.tier === "uncertain");
  const n = (t: string) => doc.items.filter((it) => it.tier === t).length;
  listEl.innerHTML =
    `<summary>冠水想定箇所（${doc.items.length} か所）</summary>` +
    `<p class="note">${esc(doc.processing)}。<b>埼玉・群馬・長野は未収録</b>です（資料の地図から位置を十分に読み取れないため）。</p>` +
    `<ul class="note"><li>点（${n("confirmed")}）: 位置を確かめられたもの。経路の計算にも使っています</li>` +
    `<li>破線の円（${n("uncertain")}）: 位置は未確認。経路の計算には使っていません。説明は<b>円の中心の白い丸</b>を押すと開きます（円の中は出発地・目的地に使えます）</li>` +
    `<li>地図に無いもの（${n("unlocated")}）: 位置を決められませんでした（下の一覧）</li></ul>` +
    (un.length + unc.length
      ? `<p class="note"><b>位置が未確認・不明のもの</b>（資料で確かめられます）</p><ul class="note rk-un">` +
        [...unc, ...un]
          .map((it) => `<li>${esc(it.place)}${it.route ? `（${esc(it.route)}）` : ""}<br>` +
            (it.src.url
              ? `<a href="${encodeURI(it.src.url)}#page=${it.src.page}" target="_blank" rel="noopener noreferrer">${esc(it.src.hint)}</a>`
              : esc(it.src.hint)) +
            `${it.tier === "uncertain" ? "（地図に破線の円）" : ""}</li>`)
          .join("") + "</ul>"
      : "");
  listEl.hidden = false;
}
