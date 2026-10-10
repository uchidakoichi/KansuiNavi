/**
 * 地図表示。MapLibre GL JS v6 は ESM 専用なので named import で使う
 * （`import maplibregl from "maplibre-gl"` は v6 で動かない）。
 *
 * ★T-13 を継承: v6 はワーカーを別ファイルとして実行時に取りに行く。
 *   `vite.config.ts` の `kansui:maplibre-worker` プラグインが
 *   `assets/` へ**ハッシュ無しの固定名**で出す。**黙って壊れるので消さない。**
 *   dev では再現しない。**必ず build + preview で見ること。**
 */

import {
  Map as MLMap,
  NavigationControl,
  type ExpressionSpecification,
  type GeoJSONSource,
  type StyleSpecification,
} from "maplibre-gl";
import type { FeatureCollection } from "geojson";

const EMPTY: FeatureCollection = { type: "FeatureCollection", features: [] };

/** ★地理院タイルは 256px。MapLibre の既定は 512 なので明示しないとズームが1段ずれる */
const GSI_TILE_SIZE = 256;

/**
 * ★出典表示は常時（絶対ルール7）。編集・加工した旨も規約が求めている。
 * ★道幅は「国土地理院最適化ベクトルタイル」の幅員区分から作っている（NK-18）。
 *   公式リポジトリ（github.com/gsi-cyberjapan/optimal_bvmap）が出典の明示を求めている
 *   （2026-09-24 のレビューで欠けていたと判明）。
 */
const ATTRIB =
  '<a href="https://maps.gsi.go.jp/development/ichiran.html" target="_blank" rel="noopener">国土地理院</a>' +
  "（地図・標高・ベクトルタイルを加工）" +
  ' | © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors (ODbL)';

/**
 * ★NK-09: 道路冠水想定箇所の出典。**そのデータを地図に出すレイヤの `attribution` に付ける**
 *   （MapLibre はレイヤが表示されているときだけ出典を出す）。データを取り込むまでは使われない。
 *   ★「国が作成したかのような態様」にしない（PDL1.0）。加工したことを必ず書く。
 */
export const ROAD_KANSUI_ATTRIB =
  // ★2026-10-02: 座標付きの公式配布は無く、各道路管理者の PDF から本アプリが位置を推定した（NK-09）
  // ★地図の上に常時出るので短くする。詳しい出典はパネルの「データの出典と利用条件」に書いてある
  "冠水想定箇所: 国土交通省等の公表資料を本アプリが加工";

/**
 * ★浸水想定区域の重ね表示（2026-10-11・利用者判断）。ハザードマップポータルサイトの**全国統合版**タイルを
 *   その場で読む（opendata.html「URLからリアルタイムに読み込み」。商用・非商用を問わず利用可）。
 * ★**経路の計算には使わない**（`hazard.w2_hazard: 0.0`。藤沢版の決定を継承）。重ねて見るだけ。
 * ★データの無いタイルは 404 が返る（2026-10-11 実測）。→ 下の `onError` から外す（画面にエラーを出さない）
 */
const LSLD_ATTRIB =
  '土砂災害警戒区域等: <a href="https://disaportal.gsi.go.jp/" target="_blank" rel="noopener">ハザードマップポータルサイト</a>' +
  "（「国土数値情報 土砂災害警戒区域データ」〈国土交通省〉をもとに国土地理院が加工。原典: 各都道府県）";
export const HAZARD_LAYERS = {
  flood_l2: {
    url: "https://disaportaldata.gsi.go.jp/raster/01_flood_l2_shinsuishin_data/{z}/{x}/{y}.png",
    attribution:
      '洪水浸水想定区域: <a href="https://disaportal.gsi.go.jp/" target="_blank" rel="noopener">ハザードマップポータルサイト</a>（国土交通省各地方整備局等・都道府県）',
  },
  naisui: {
    url: "https://disaportaldata.gsi.go.jp/raster/02_naisui_data/{z}/{x}/{y}.png",
    attribution:
      '内水浸水想定区域: <a href="https://disaportal.gsi.go.jp/" target="_blank" rel="noopener">ハザードマップポータルサイト</a>（市町村）',
  },
  // ★高潮は 18 都道府県だけ（opendata.html。作成中の都道府県がある）→ hazard-coverage.ts の HIGHTIDE_PREFS
  hightide: {
    url: "https://disaportaldata.gsi.go.jp/raster/03_hightide_l2_shinsuishin_data/{z}/{x}/{y}.png",
    attribution:
      '高潮浸水想定区域: <a href="https://disaportal.gsi.go.jp/" target="_blank" rel="noopener">ハザードマップポータルサイト</a>（都道府県）',
  },
  // ★土砂災害警戒区域等は現象ごとに3枚（全国・2026-08-06 から）。「国土数値情報 土砂災害警戒区域（令和7年度）」を国土地理院が加工。
  //   ★原典の都道府県ごとの条件あり（KsjTmplt-A33-2025 の A33_permision_R7.xlsx）: 京都府は商用利用不可、鳥取・広島は概略位置 ほか
  lsld_dosekiryu: { url: "https://disaportaldata.gsi.go.jp/raster/05_dosekiryukeikaikuiki/{z}/{x}/{y}.png", attribution: LSLD_ATTRIB },
  lsld_kyukeisha: { url: "https://disaportaldata.gsi.go.jp/raster/05_kyukeishakeikaikuiki/{z}/{x}/{y}.png", attribution: LSLD_ATTRIB },
  lsld_jisuberi: { url: "https://disaportaldata.gsi.go.jp/raster/05_jisuberikeikaikuiki/{z}/{x}/{y}.png", attribution: LSLD_ATTRIB },
} as const;
export type HazardKey = keyof typeof HAZARD_LAYERS;

import { STATE } from "./state";
/** NK-09 の表示レイヤ（roadkansui.ts が足す）。地図のクリックで出発地にしない */
export const RK_LAYERS = ["rk-hit-uncertain", "rk-hit-confirmed"];
export { STATE };

/**
 * ★★**`glyphs` のキー自体を書かない。** `glyphs: undefined` と書くと
 *   MapLibre の検証が `glyphs: string expected, undefined found` で落ち、
 *   `Style._load` が **`return` して何も読み込まない**（v6.10.0 で実装を確認）。
 *
 *   → **地図が真っ白になる。** 例外は投げられず、`console` に1行出るだけ。
 *     出典表示（絶対ルール7）も消える。ソースもレイヤも作られないので、
 *     `map.on("load")` も発火せず `fit()` も呼ばれない。
 *
 *   ★2026-09-21 に実機で出た。**開発機で気づけなかったのは、
 *     描画結果を一度も見ていなかったから。** DOM を調べると
 *     `maplibregl-canvas` は存在するので「地図がある」ように見える。
 *     → **画面を見て確かめること。** 加えて下の `onError` で表に出す。
 *
 *   文字を出すレイヤ（symbol）はこの版に無いので `glyphs` 自体が要らない。
 */
function baseStyle(): StyleSpecification {
  return {
    version: 8,
    sources: {
      pale: {
        type: "raster",
        tiles: ["https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png"],
        tileSize: GSI_TILE_SIZE,
        minzoom: 2,
        maxzoom: 18,
        attribution: ATTRIB,
      },
      ...Object.fromEntries(
        Object.entries(HAZARD_LAYERS).map(([k, v]) => [
          k,
          { type: "raster" as const, tiles: [v.url], tileSize: GSI_TILE_SIZE, minzoom: 2, maxzoom: 17, attribution: v.attribution },
        ]),
      ),
      route: { type: "geojson", data: EMPTY },
      baseline: { type: "geojson", data: EMPTY },
      meshes: { type: "geojson", data: EMPTY },
      overlay: { type: "geojson", data: EMPTY },
      coverage: { type: "geojson", data: EMPTY },
      ends: { type: "geojson", data: EMPTY },
      // F-08 一覧で選んだ区間の強調
      focus: { type: "geojson", data: EMPTY },
    },
    layers: [
      { id: "bg", type: "background", paint: { "background-color": "#eef1f4" } },
      { id: "pale", type: "raster", source: "pale" },
      // ★浸水想定区域は道路の線より下に置く（経路と黄〜赤の線を隠さない）。既定は出さない＝タイルを取りに行かない
      ...(Object.keys(HAZARD_LAYERS) as HazardKey[]).map((k) => ({
        id: k,
        type: "raster" as const,
        source: k,
        paint: { "raster-opacity": 0.6 },
        layout: { visibility: "none" as const },
      })),

      // ★★対象範囲の外周（配布データに区画がある範囲）。
      //   この外をタップすると「対象範囲の外」と断る。**同じ区画の集合から描く**ので
      //   線の内外と判定が必ず一致する（→ mesh.ts の meshOutline）。
      //   ★読み込んだ区画の枠（下）と混同しないよう、太く・色を変える。
      {
        id: "coverage",
        type: "line",
        source: "coverage",
        paint: { "line-color": "#8b1a4a", "line-width": 2, "line-dasharray": [4, 3], "line-opacity": 0.8 },
      },

      // ★読み込んだ区画の枠。F-11 が効いていることを**目で確かめられる**ようにする
      {
        id: "meshes",
        type: "line",
        source: "meshes",
        paint: { "line-color": "#7a8a99", "line-width": 1, "line-dasharray": [2, 3] },
      },

      // ★読み込み済みの区画の点灯区間（黄〜赤）と判定不能（濃い灰色）。経路の下に薄く重ねる（2026-10-08）
      {
        id: "overlay-unknown",
        type: "line",
        source: "overlay",
        filter: ["==", ["get", "kind"], "unknown"],
        paint: { "line-color": "#3b4c5a", "line-width": 1.6, "line-opacity": 0.5 },
        layout: { "line-cap": "round" },
      },
      {
        id: "overlay-risk",
        type: "line",
        source: "overlay",
        filter: ["==", ["get", "kind"], "risk"],
        paint: {
          "line-width": 2,
          "line-opacity": 0.5,
          "line-color": ["interpolate", ["linear"], ["get", "risk"], 0.0, "#f6c445", 0.5, "#ef8a2b", 0.8, "#c8341f"] as ExpressionSpecification,
        },
        layout: { "line-cap": "round" },
      },

      // 最速経路（λ=0・除外なし）。★色だけで区別しない。破線にする
      {
        id: "baseline",
        type: "line",
        source: "baseline",
        paint: { "line-color": "#5a6b79", "line-width": 3, "line-dasharray": [3, 2] },
        layout: { "line-cap": "round" },
      },

      // ★経路の縁取り（2026-10-08）。経路の線も周辺に重ねた線も同じ黄〜赤で塗るので、重なると
      //   **太さの違いでしか見分けられなかった**（利用者の指摘）。白い太線＋濃い外縁で囲み、色に頼らず経路だと分かるようにする
      {
        id: "route-casing-outer",
        type: "line",
        source: "route",
        paint: { "line-color": "#14202c", "line-width": 11, "line-opacity": 0.85 },
        layout: { "line-cap": "round", "line-join": "round" },
      },
      {
        id: "route-casing",
        type: "line",
        source: "route",
        paint: { "line-color": "#ffffff", "line-width": 8.5 },
        layout: { "line-cap": "round", "line-join": "round" },
      },
      // 経路本体。リスクの強さで色を変える
      {
        id: "route",
        type: "line",
        source: "route",
        paint: {
          "line-width": 5,
          "line-color": [
            "case",
            // ★★**採点できたかどうかを最初に見る。**
            //   `state` で判定不能を見分けていたが、採点できなかった区間は
            //   `score = 255` なのに `state = topo` のままなので取りこぼしていた
            //   （2026-09-22 の監査。神奈川で 946本が**最も安全な色**で描かれていた）。
            //   ★`scored` は「S_topo を採点できたか」そのもの。**ここを起点にする。**
            ["!", ["get", "scored"]],
            "#3b4c5a", // ★判定不能。4段階の色に混ぜない
            ["==", ["get", "state"], STATE.UNDERPASS],
            "#3b4c5a", // ★（上で拾えるが、意味を残すため明示しておく）
            ["==", ["get", "state"], STATE.KNOWN],
            "#8b1a4a", // 道路冠水想定箇所（NK-09。各道路管理者の公表データ）
            [
              "interpolate",
              ["linear"],
              ["get", "risk"],
              0.0,
              "#2f7fb8",
              0.2,
              "#f6c445",
              0.5,
              "#ef8a2b",
              0.8,
              "#c8341f",
            ] as ExpressionSpecification,
          ] as ExpressionSpecification,
        },
        layout: { "line-cap": "round", "line-join": "round" },
      },

      // ★F-08 で選んだ区間。経路の色を隠さないよう、太い半透明の縁取りにする
      {
        id: "focus",
        type: "line",
        source: "focus",
        paint: { "line-color": "#14202c", "line-width": 12, "line-opacity": 0.35 },
        layout: { "line-cap": "round", "line-join": "round" },
      },

      {
        id: "ends",
        type: "circle",
        source: "ends",
        paint: {
          "circle-radius": 7,
          "circle-color": ["case", ["==", ["get", "kind"], "from"], "#1c6ea4", "#8b1a4a"],
          "circle-stroke-color": "#fff",
          "circle-stroke-width": 2,
        },
      },
    ],
  };
}

export interface MapHandles {
  map: MLMap;
  setRoute(fc: FeatureCollection): void;
  setBaseline(fc: FeatureCollection): void;
  setMeshes(fc: FeatureCollection): void;
  setOverlay(fc: FeatureCollection): void;
  setCoverage(fc: FeatureCollection): void;
  setEnds(fc: FeatureCollection): void;
  /** F-08 で選んだ区間を強調し、そこへ寄せる。null で消す */
  focusSegment(lines: [number, number][][] | null): void;
  /** 浸水想定区域の重ね表示を出す・消す */
  setHazard(key: HazardKey, on: boolean): void;
  /**
   * 面で持っている浸水想定区域（ポータルに無い市区町村。例: 藤沢市の内水）を出す・消す。
   * ★初めて出すときに `url` を読む（使わない人には通信させない）。色は面の `color` 属性
   */
  setPolygonOverlay(id: string, url: string, attribution: string, on: boolean): void;
  fit(bbox: [number, number, number, number]): void;
}

export function createMap(
  container: string,
  center: [number, number],
  onClick: (lon: number, lat: number) => void,
  onError?: (message: string) => void,
): MapHandles {
  const map = new MLMap({
    container,
    style: baseStyle(),
    center,
    zoom: 12,
    attributionControl: { compact: false },
  });
  map.addControl(new NavigationControl({ showCompass: false }), "top-right");
  map.on("click", (e) => {
    // ★NK-09 の印を押したときは説明を出す（出発地・目的地にしない）
    const rk = RK_LAYERS.filter((id) => map.getLayer(id));
    if (rk.length && map.queryRenderedFeatures(e.point, { layers: rk }).length) return;
    onClick(e.lngLat.lng, e.lngLat.lat);
  });
  // ★MapLibre の失敗を**画面に出す**。console だけだと実機で気づけない。
  //   スタイル検証の失敗はここに ErrorEvent として届く（上の `glyphs` の件）。
  map.on("error", (e) => {
    // ★浸水想定区域のタイルは、区域の無いところで 404 が返る（正常）。エラーとして出さない
    const sid = (e as unknown as { sourceId?: string }).sourceId;
    if (sid && sid in HAZARD_LAYERS) return;
    onError?.(String(e?.error?.message ?? e?.error ?? "不明なエラー"));
  });

  const src = (id: string) => map.getSource(id) as GeoJSONSource | undefined;
  return {
    map,
    setRoute: (fc) => src("route")?.setData(fc),
    setBaseline: (fc) => src("baseline")?.setData(fc),
    setMeshes: (fc) => src("meshes")?.setData(fc),
    setOverlay: (fc) => src("overlay")?.setData(fc),
    setCoverage: (fc) => src("coverage")?.setData(fc),
    setHazard: (key, on) => map.setLayoutProperty(key, "visibility", on ? "visible" : "none"),
    setPolygonOverlay: (id, url, attribution, on) => {
      if (!map.getSource(id)) {
        if (!on) return;
        map.addSource(id, { type: "geojson", data: url, attribution });
        // ★道路の線より下（「coverage」の前）に置く。ポータルのタイルと同じ不透明度
        map.addLayer(
          { id, type: "fill", source: id, paint: { "fill-color": ["get", "color"], "fill-opacity": 0.6 } },
          "coverage",
        );
        return;
      }
      map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
    },
    setEnds: (fc) => src("ends")?.setData(fc),
    focusSegment: (lines) => {
      src("focus")?.setData(
        lines
          ? { type: "Feature", properties: {}, geometry: { type: "MultiLineString", coordinates: lines } }
          : { type: "FeatureCollection", features: [] },
      );
      if (!lines) return;
      const pts = lines.flat();
      const xs = pts.map((p) => p[0]);
      const ys = pts.map((p) => p[1]);
      map.fitBounds(
        [
          [Math.min(...xs), Math.min(...ys)],
          [Math.max(...xs), Math.max(...ys)],
        ],
        { padding: 80, maxZoom: 16, duration: 400 },
      );
    },
    fit: (b) =>
      map.fitBounds(
        [
          [b[0], b[1]],
          [b[2], b[3]],
        ],
        { padding: 60, duration: 400 },
      ),
  };
}
