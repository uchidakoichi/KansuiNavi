import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { defineConfig, type Plugin } from "vite";

const require = createRequire(import.meta.url);

/**
 * ★MapLibre v6 のワーカーをビルド出力に置く。
 *
 * v6 はワーカーを ESM の別ファイル（maplibre-gl-worker.mjs）として持ち、
 * 実行時に `new URL("./maplibre-gl-worker.mjs", import.meta.url)` で取りに行く。
 * バンドルすると import.meta.url は dist/assets/index-*.js を指すため、
 * **同じ場所にこのファイルが無いとワーカーが起動しない。**
 *
 * ★これは黙って壊れる。ワーカーが起動しないと GeoJSON ソースが一切読み込まれず、
 *   経路線・リスク線・アンダーパス・既知冠水箇所が**すべて描画されない**まま、
 *   例外もコンソールエラーも出ずに地図だけが表示される。
 *   dev（vite）では node_modules から直接配られるため再現しない。
 *   2026-09-05 に本番ビルドで実機確認して発見した。
 *
 * ワーカーは maplibre-gl-shared.mjs を import するので、2つとも同じ場所に置く。
 */
function maplibreWorker(): Plugin {
  const dist = dirname(require.resolve("maplibre-gl/dist/maplibre-gl.mjs"));
  const files = ["maplibre-gl-worker.mjs", "maplibre-gl-shared.mjs"];
  return {
    name: "kansui:maplibre-worker",
    apply: "build",
    generateBundle() {
      for (const f of files) {
        this.emitFile({
          type: "asset",
          // ★ハッシュを付けない。MapLibre はこの名前で固定して取りに行く
          fileName: `assets/${f}`,
          source: readFileSync(join(dist, f)),
        });
      }
    },
  };
}

export default defineConfig({
  // GitHub Pages / Cloudflare Pages のサブパス配信でも動くよう相対パスにする
  base: "./",
  optimizeDeps: {
    // maplibre-gl v6 は Worker を ESM で内包しており、dep optimizer が
    // maplibre-gl-worker.mjs を取りこぼす。事前バンドル対象から外す。
    exclude: ["maplibre-gl"],
  },
  plugins: [maplibreWorker()],
});
