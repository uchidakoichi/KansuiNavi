/**
 * F-08 経路上の注意地点一覧（藤沢版 `web/src/hazards.ts` の規則を継承）。
 *
 * ワーカーが返す経路の GeoJSON（エッジごとに risk / scored / state / name）を
 * 先頭から歩き、同じ種類の区間が続く間をひとまとまりに畳む。
 *
 * ★種類を混ぜない。判定不能・既知冠水箇所・地形推定は**根拠の性質が違う**。
 * ★閾値を下回った1本で切らない。mergeGapM までの途切れは同じまとまりとする。
 * ★★全国版は**ノード名（交差点名）を持たない**。地点は「道路名＋出発からの距離」で書く。
 *   藤沢版の「遠くの交差点名を借りない」と同じ筋で、**名前を作らない**。
 */

import type { Feature, FeatureCollection, LineString } from "geojson";
import { STATE } from "./state";

/**
 * ★判定不能は理由で分ける（2026-09-27、大東諸島で「アンダーパス」と誤って出ていた）:
 *   unknown … 立体交差の下（アンダーパス・高架下）。DEM が橋の路面を測るので地形から判定できない
 *   nodata  … 標高データが無い（DEM5A〜5C が整備されていない。DEM10B は T-07 で外している）
 */
export type HazardKind = "unknown" | "nodata" | "known" | "topo" | "topolow";

export interface HazardItem {
  kind: HazardKind;
  /** 経路上での開始・終了位置［m］（出発地からの道なり距離） */
  fromM: number;
  toM: number;
  /** 区間の線（途切れを含みうるので複数本） */
  lines: [number, number][][];
  /** 道路名。無ければ null */
  road: string | null;
  /** この区間で最も高い risk（topo のときだけ意味を持つ） */
  peak: number;
}

export interface HazardOptions {
  mergeGapM: number;
}

interface EdgeProps {
  risk: number;
  scored: boolean;
  state: number;
  name: string | null;
  /** ★DEM10B（精度の低い標高）で推定した区間（NK-07） */
  demLow?: boolean;
}

function meters(a: number[], b: number[]): number {
  const cy = Math.cos((((a[1] + b[1]) / 2) * Math.PI) / 180);
  return Math.hypot((a[0] - b[0]) * cy, a[1] - b[1]) * 111320;
}

/**
 * ★`scored` を最初に見る（map.ts の色分けと同じ順）。採点できなかった区間は
 *   `state = topo` のままなので、state だけで見ると「最も安全」に落ちる。
 * ★risk はワーカーで既に閾値を引いてある（risk > 0 ＝ 雨の強さスライダで点灯）。
 */
function classify(p: EdgeProps): HazardKind | null {
  if (p.state === STATE.KNOWN) return "known";
  if (p.state === STATE.UNDERPASS) return "unknown";
  if (!p.scored) return "nodata";
  if (!(p.risk > 0)) return null;
  // ★根拠の精度が違う区間を1つに畳まない（NK-07・T-11）
  return p.demLow ? "topolow" : "topo";
}

export function buildHazardList(fc: FeatureCollection, opt: HazardOptions): HazardItem[] {
  const runs: (HazardItem & { tally: Map<string, number>; lastEnd: number[] | null })[] = [];
  let at = 0;
  for (const f of fc.features as Feature<LineString, EdgeProps>[]) {
    const [a, b] = f.geometry.coordinates;
    const len = meters(a, b);
    const p = f.properties;
    const kind = classify(p);
    if (kind) {
      const last = runs.length ? runs[runs.length - 1] : null;
      if (last && last.kind === kind && at - last.toM <= opt.mergeGapM) {
        last.toM = at + len;
        // 連続していれば同じ線に足す。途切れていれば新しい線にする
        if (last.lastEnd && last.lastEnd[0] === a[0] && last.lastEnd[1] === a[1]) {
          last.lines[last.lines.length - 1].push([b[0], b[1]]);
        } else {
          last.lines.push([[a[0], a[1]], [b[0], b[1]]]);
        }
        last.lastEnd = b;
        if (p.risk > last.peak) last.peak = p.risk;
      } else {
        runs.push({
          kind,
          fromM: at,
          toM: at + len,
          lines: [[[a[0], a[1]], [b[0], b[1]]]],
          road: null,
          peak: p.scored ? p.risk : 0,
          tally: new Map(),
          lastEnd: b,
        });
      }
      const r = runs[runs.length - 1];
      if (p.name) r.tally.set(p.name, (r.tally.get(p.name) ?? 0) + len);
    }
    at += len;
  }

  // 道路名はまとまりの中で最も長く続くものを採る
  const out: HazardItem[] = runs.map(({ tally, lastEnd: _, ...r }) => {
    let best: string | null = null;
    let bv = 0;
    for (const [k, v] of tally) if (v > bv) [best, bv] = [k, v];
    return { ...r, road: best };
  });

  // 危険な順に。既知冠水箇所 → 判定不能（立体交差の下 → 標高データなし）→ 地形推定（risk の高い順）
  const rank: Record<HazardKind, number> = { known: 0, unknown: 1, nodata: 2, topo: 3, topolow: 3 };
  out.sort((a, b) => rank[a.kind] - rank[b.kind] || b.peak - a.peak || a.fromM - b.fromM);
  return out;
}

export function fmtLen(m: number): string {
  // ★「0m」と書かない（短い区間を「無い」と読ませない）
  if (m < 10) return "10m未満";
  return m < 1000 ? `${Math.round(m / 10) * 10}m` : `${(m / 1000).toFixed(1)}km`;
}

/** 行の見出し。★名前を持たないので距離で書く */
export function hazardWhere(h: HazardItem): string {
  return `出発から約${fmtLen(h.fromM)} 地点`;
}

/** 行の本文。★根拠の違いをここで言い分ける。「安全」という語は使わない（ルール5） */
export function hazardWhat(h: HazardItem): string {
  // ★NK-09: 全国版の出典は「道路冠水想定箇所」（各道路管理者の公表データ）。
  //   藤沢版の「過去の冠水実績あり」は意味が違うので使わない
  if (h.kind === "known") return "道路冠水注意箇所（道路管理者の公表資料。位置は本アプリが推定）";
  if (h.kind === "unknown") return "アンダーパス・高架下など（地形からは判定できません）";
  if (h.kind === "nodata") return "標高データが無い区間（地形からは判定できません）";
  if (h.kind === "topolow") return "周囲より低い区間（精度の低い10mメッシュの標高からの推定）";
  return "周囲より低い区間（地形からの推定）";
}
