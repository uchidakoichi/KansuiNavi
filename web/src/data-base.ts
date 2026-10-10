/**
 * 配布データ（index.json・区画・places.json・road_kansui.json）の置き場（2026-10-06）。
 *
 * ★アプリ本体と配布データは**別の Cloudflare Pages プロジェクト**に置く（利用者判断 2026-10-06）。
 *   アプリは GitHub の master へのプッシュで自動デプロイ、データ（約590MB・git に入れない）は手元から手動デプロイ。
 * ★置き場はビルド時の環境変数 `VITE_DATA_BASE`（例 `https://<データのプロジェクト>.pages.dev/data`）で渡す。
 *   コードに直書きしない。**未設定なら同じサイトの `data/`**（手元の preview・藤沢版と同じ形）。
 * ★別のオリジンから読むので、データ側の `_headers` で CORS を許す（tools/data-site/_headers）。
 */
const RAW = (import.meta.env.VITE_DATA_BASE as string | undefined)?.trim();

/** データの置き場（末尾の / なしの絶対 URL）。ワーカーにもこれを渡す */
export const DATA_BASE = new URL(RAW || "data", location.href).href.replace(/\/$/, "");

/** データのファイルの絶対 URL */
export function dataUrl(name: string): string {
  return `${DATA_BASE}/${name}`;
}
