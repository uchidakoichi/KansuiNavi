/**
 * F-03 ルート標高プロファイル（藤沢版 `web/src/profile.ts` を全国版の経路の形に合わせたもの）。
 *
 * ★立体交差では DEM が橋の路面を拾っているため、プロファイルが実際の路面と食い違う。
 *   その区間（判定不能）を灰色の帯で示し、「ここは地形から判定できない」と明示する。
 *   なめらかな線に紛れ込ませると、最も危険な場所が最も平穏に見えてしまう。
 * ★標高が不明のノードは線を切る（0m にしない。T-09）。
 *
 * ★2026-09-21 に藤沢版で取り下げ、2026-09-24 に国土地理院の回答
 *   （標高タイルは申請不要・出典の明示で利用可）を受けて戻した。
 */
import type { Feature, FeatureCollection, LineString } from "geojson";
import { STATE } from "./state";

const R = 6371000;
function haversine(lo1: number, la1: number, lo2: number, la2: number): number {
  const p = Math.PI / 180;
  const a =
    Math.sin(((la2 - la1) * p) / 2) ** 2 +
    Math.cos(la1 * p) * Math.cos(la2 * p) * Math.sin(((lo2 - lo1) * p) / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

interface EdgeProps {
  scored: boolean;
  state: number;
  bridge?: boolean;
  tunnel?: boolean;
}

/** 描けたら true（標高が1点も無ければ false） */
export function drawProfile(
  cv: HTMLCanvasElement,
  fc: FeatureCollection,
  elev: (number | null)[],
  elevLow: boolean[] = [],
): boolean {
  const ctx = cv.getContext("2d");
  const feats = fc.features as Feature<LineString, EdgeProps>[];
  if (!ctx || !feats.length || elev.length !== feats.length + 1) return false;
  const css = getComputedStyle(document.documentElement);
  const ink = css.getPropertyValue("--fg").trim() || "#1b2733";
  const muted = css.getPropertyValue("--muted").trim() || "#5a6b79";
  const alert = css.getPropertyValue("--alert").trim() || "#8b1a4a";

  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth || 300;
  const H = 150;
  cv.width = W * dpr;
  cv.height = H * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  // ノード i の道なり距離（ノード i は i 本目の区間の始点。最後は最後の区間の終点）
  const xs: number[] = [0];
  for (const f of feats) {
    const [a, b] = f.geometry.coordinates;
    xs.push(xs[xs.length - 1] + haversine(a[0], a[1], b[0], b[1]));
  }
  const acc = xs[xs.length - 1];
  // ★★橋の区間は、途中のノードの標高を**両端を結んだ直線**に置き換え、破線で描く。
  //   標高タイルは橋の路面ではなく橋の下の地面（川床・谷底）を測るので、そのまま描くと
  //   実在しない谷が出る（藤沢版の神川橋で約10m。2026-09-24 のレビューで指摘）。
  // ★★トンネルも同じ扱い（2026-09-29・利用者判断）。標高タイルは地表（トンネルの上の山）を
  //   測るので、そのまま描くと実在しない山が出る（札幌→旭川で 478m まで跳ねた）
  const isBridge = (i: number) =>
    feats[i].properties.bridge === true || feats[i].properties.tunnel === true;
  const ys: (number | null)[] = elev.slice();
  for (let i = 0; i < feats.length; ) {
    if (!isBridge(i)) {
      i++;
      continue;
    }
    let j = i;
    while (j + 1 < feats.length && isBridge(j + 1)) j++;
    const a = elev[i];
    const b = elev[j + 1];
    for (let k = i + 1; k <= j; k++) {
      ys[k] = a === null || b === null
        ? null
        : a + ((b - a) * (xs[k] - xs[i])) / Math.max(xs[j + 1] - xs[i], 1e-9);
    }
    i = j + 1;
  }
  // ★軸の上下限も補間後の値で決める（橋の下の谷で目盛りを広げない）
  const valid = ys.filter((v): v is number => v !== null);
  if (!valid.length || acc === 0) return false;
  const lo = Math.min(...valid);
  const hi = Math.max(...valid);
  const pad = 18;
  const span = Math.max(hi - lo, 2);
  const px = (d: number) => pad + (d / acc) * (W - pad * 2);
  const py = (e: number) => H - pad - ((e - lo) / span) * (H - pad * 2);

  // 判定不能区間の帯を先に敷く（★scored を最初に見る。map.ts の色分けと同じ順）
  ctx.fillStyle = muted + "33";
  feats.forEach((f, i) => {
    if (f.properties.scored && f.properties.state !== STATE.UNDERPASS) return;
    ctx.fillRect(px(xs[i]), pad - 8, Math.max(px(xs[i + 1]) - px(xs[i]), 2), H - pad * 2 + 16);
  });

  ctx.strokeStyle = muted + "55";
  ctx.lineWidth = 1;
  for (let k = 0; k <= 2; k++) {
    const y = pad + ((H - pad * 2) / 2) * k;
    ctx.beginPath();
    ctx.moveTo(pad, y);
    ctx.lineTo(W - pad, y);
    ctx.stroke();
  }

  ctx.strokeStyle = ink;
  ctx.lineWidth = 1.8;
  for (let i = 0; i < feats.length; i++) {
    const y0 = ys[i];
    const y1 = ys[i + 1];
    if (y0 === null || y1 === null) continue; // ★不明は線を切る（0m にしない）
    // ★橋は両端を結んだ破線。★DEM10B（精度の低い標高・NK-07）は細かい点線で区別する
    ctx.setLineDash(isBridge(i) ? [4, 3] : elevLow[i] || elevLow[i + 1] ? [1.5, 2.5] : []);
    ctx.beginPath();
    ctx.moveTo(px(xs[i]), py(y0));
    ctx.lineTo(px(xs[i + 1]), py(y1));
    ctx.stroke();
  }
  ctx.setLineDash([]);

  // 既知冠水箇所は別マーカー
  ctx.fillStyle = alert;
  feats.forEach((f, i) => {
    if (f.properties.state !== STATE.KNOWN) return;
    ctx.beginPath();
    ctx.arc(px((xs[i] + xs[i + 1]) / 2), pad - 4, 4, 0, Math.PI * 2);
    ctx.fill();
  });

  ctx.fillStyle = muted;
  ctx.font = "11px ui-monospace, monospace";
  ctx.fillText(`${hi.toFixed(1)}m`, 2, pad - 4);
  ctx.fillText(`${lo.toFixed(1)}m`, 2, H - pad + 12);
  ctx.textAlign = "right";
  ctx.fillText(`${(acc / 1000).toFixed(2)}km`, W - 2, H - pad + 12);
  return true;
}
