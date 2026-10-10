/**
 * F-01 住所・交差点名の検索（全国版）。NK-12 決定 2026-09-28: **OSM を同梱**して端末内で引く。
 *
 * ★地理院の住所検索 API は使わない（仕様変更のたびに保守が要る。国土地理院も
 *   「主に地理院地図からの利用を想定」「予告なく変更する場合がある」としている）。
 * ★データ（`data/places.json`、全国で数MB）は**検索欄を使い始めたときに初めて読む**。
 *   起動を重くしない。オフラインでも Service Worker の data キャッシュから読める。
 * ★探せるのは町丁目・大字・地区と、名前の付いた信号交差点だけ。**番地・よみがなでは探せない。**
 *   網羅は保証しない（地域でばらつく）。画面に書く。
 * ★同名が全国に多い（「本町」等）。候補ごとに「都道府県 市区町村」を出す。
 *   「藤沢市 羽鳥」のように市区町村名を前に付けて絞り込める。
 */

export interface PlaceData {
  version: number;
  maxResults: number;
  kinds: string[];
  munis: string[];
  name: string[];
  k: number[];
  m: number[];
  lon: number[];
  lat: number[];
}

export interface Hit {
  name: string;
  kind: string;
  muni: string;
  lon: number;
  lat: number;
}

/** 表記ゆれを吸収する。全角英数・大小文字・空白を畳む（藤沢版と同じ） */
export const norm = (t: string) => t.normalize("NFKC").toLowerCase().replace(/\s+/g, "");

export class PlaceIndex {
  private d: PlaceData;
  private names: string[];
  /** 「市区町村＋名前」。市区町村名を前に付けた入力で引くため */
  private full: string[];
  constructor(d: PlaceData) {
    this.d = d;
    this.names = d.name.map(norm);
    // ★都道府県・市・区まで含めた全体（「横浜市 港北」「神奈川県 羽鳥」でも引けるように。7回目のレビュー）
    this.full = d.name.map((n, i) => norm(d.munis[d.m[i]] + n));
  }

  get size() {
    return this.d.name.length;
  }

  hit(i: number): Hit {
    const d = this.d;
    return {
      name: d.name[i],
      kind: d.kinds[d.k[i]],
      muni: d.munis[d.m[i]],
      lon: d.lon[i] / 1e5,
      lat: d.lat[i] / 1e5,
    };
  }

  /**
   * その地点に最も近い地名の市区町村（「都道府県 市区町村」）。`maxKm` より遠ければ null。
   * ★行政区域の面は持っていないので、**境の近くでは隣の市区町村を返すことがある**（内水の配信対象の目安に使う）。
   */
  nearestMuni(lon: number, lat: number, maxKm: number): string | null {
    const d = this.d;
    const k = Math.cos((lat * Math.PI) / 180);
    let best = -1;
    let bd = Infinity;
    for (let i = 0; i < d.lon.length; i++) {
      const dx = (d.lon[i] / 1e5 - lon) * k;
      const dy = d.lat[i] / 1e5 - lat;
      const q = dx * dx + dy * dy;
      if (q < bd) {
        bd = q;
        best = i;
      }
    }
    return best >= 0 && Math.sqrt(bd) * 111 <= maxKm ? d.munis[d.m[best]] : null;
  }

  /**
   * 名前の前方一致 → 名前の部分一致 → 「市区町村＋名前」の前方一致・部分一致 の順。
   * ★件数は `maxResults` で打ち切る。**打ち切ったことを返す**（黙って落とさない）。
   */
  search(query: string): { hits: Hit[]; more: boolean } {
    // ★空白で区切った語を**すべて含む**ものを探す（市区町村名と町名を別々に入れてよい）
    const toks = query.split(/[\s\u3000]+/).map(norm).filter(Boolean);
    if (!toks.length) return { hits: [], more: false };
    const last = toks[toks.length - 1];
    const max = this.d.maxResults;
    const all = (i: number) => toks.every((t) => this.full[i].includes(t));
    const tiers: ((i: number) => boolean)[] =
      toks.length === 1
        ? [(i) => this.names[i].startsWith(last), (i) => this.names[i].includes(last), all]
        : [(i) => all(i) && this.names[i].startsWith(last), all];
    const seen = new Set<number>();
    const out: number[] = [];
    let more = false;
    for (const t of tiers) {
      for (let i = 0; i < this.names.length; i++) {
        if (seen.has(i) || !t(i)) continue;
        if (out.length >= max) {
          more = true;
          break;
        }
        seen.add(i);
        out.push(i);
      }
      if (more) break;
    }
    return { hits: out.map((i) => this.hit(i)), more };
  }
}

let loading: Promise<PlaceIndex> | null = null;

/** ★一度だけ読む。失敗したら次の入力でもう一度試せるようにする */
export function loadPlaces(url: string): Promise<PlaceIndex> {
  loading ??= fetch(url)
    .then((r) => {
      if (!r.ok) throw new Error(`places.json: ${r.status}`);
      return r.json() as Promise<PlaceData>;
    })
    .then((d) => {
      // ★配信側が HTML を 200 で返すことがある（T-14）。中身で確かめる
      if (!Array.isArray(d?.name) || d.name.length !== d.lon?.length) {
        throw new Error("places.json の中身が想定と違う");
      }
      return new PlaceIndex(d);
    })
    .catch((e) => {
      loading = null;
      throw e;
    });
  return loading;
}
