/**
 * F-09 オフライン動作（全国版）。
 *
 * ★★**藤沢版との違いはここだけ**: あちらは `graph.bin` 1本だったが、
 *   全国版は**区画（2次メッシュ）を必要なぶんだけ後から読む**（F-11）。
 *   → 区画は「使ったものだけ」溜まる。**先読みしない。**
 *     神奈川県ぶんを全部入れると 13.1MB。使わない区画まで落とすのは、
 *     豪雨のさなかの携帯回線でやってよいことではない。
 *
 * ★このファイルはビルドを通さない素の JS（`public/` にそのまま置く）。
 *   バンドルすると出力名にハッシュが付き、登録先 URL が毎回変わる。
 *   更新の検出は「同じ URL の中身が変わったか」で行われるので、
 *   URL が変わる置き方をすると**更新が届かなくなる**。
 *
 * ★エラー応答をキャッシュしない。status 200 以外と opaque は保存しない。
 *   404 を保存すると、その区画が**オフラインで永久に取れなくなる**。
 *
 * ★T-15（本番だけ壊れる）を継承。詳しくは `install` と `unredirect` の注記。
 */

// ★版は用途ごとに分ける。まとめて上げると、データを差し替えただけで
//   利用者の地図タイル（最大900枚）まで捨てさせることになる。
//
// ★**配布データの構造を変えたら DATA_VERSION を上げる。**
//   `data/index.json` は staleWhileRevalidate なので、上げないと
//   **新しいアプリ本体と古い index.json が1回ぶん噛み合う。**
//   全国版ではこれが特に危ない: `index.json` の `format` と
//   `router.worker.ts` の `DATA_FORMAT` が食い違うと**起動時に落ちる**設計なので、
//   利用者には「配布データの形式が違う」という文言だけが見える。
//
// ★**区画ファイルは版をまたいで使い回さない。** `DATA_VERSION` を上げると
//   `activate` で古いキャッシュごと消える。区画の中身（レイアウト・旗・
//   幅員の狭さ）は版で変わるので、混ぜると**黙って古い値で経路を引く。**
const SHELL_VERSION = "v7"; // アプリ本体（HTML / JS / CSS）
const DATA_VERSION = "v25"; // data/*（index.json・区画ファイル・places.json）
const TILES_VERSION = "v1"; // 地図タイル（実行時に溜まる）

const SHELL = `kansui-n-shell-${SHELL_VERSION}`;
const DATA = `kansui-n-data-${DATA_VERSION}`;
/** 配布データのオリジン（sw.js?data=...）。無ければ同じサイト */
const DATA_ORIGIN = new URL(self.location.href).searchParams.get("data") || self.location.origin;
const TILES = `kansui-n-tiles-${TILES_VERSION}`;
const MINE = new Set([SHELL, DATA, TILES]);

/** 地図タイルの保存上限［件］ */
const TILE_LIMIT = 900;
/** 一度に削る枚数。1枚ずつ削ると毎回 keys() を舐めることになる */
const TILE_TRIM = 120;

/**
 * 区画ファイルの保存上限［件］。
 *
 * ★神奈川県は全部で 46 区画なので、この地域では実質「全部入る」。
 *   ★**全国に広げたときに効く上限である。** 使い続けると際限なく増えるため。
 *   1区画あたり gz で 0.3MB 前後（最大 1.05MB）なので、120 区画で 40MB 前後。
 * ★削るのは古い順（Cache API の keys() は挿入順を返す）。
 *   消えても**次に必要になったときに取り直すだけ**で、壊れはしない。
 */
const MESH_LIMIT = 120;
const MESH_TRIM = 20;

/** タイル配信元。ここに無いホストは素通しする（勝手に溜め込まない） */
const TILE_HOSTS = new Set(["cyberjapandata.gsi.go.jp"]);

self.addEventListener("install", (e) => {
  // アプリの入口だけ先に確保する。これが無いとオフラインで何も出せない。
  //
  // ★★`"./index.html"` を入れてはいけない（T-15）。
  //   **Cloudflare Pages は `/index.html` を `/` へ 308 リダイレクトする。**
  //   `addAll` はリダイレクトを追って `redirected: true` の応答を保存するが、
  //   **その応答を画面遷移に返すことは仕様で許されていない**
  //   （"Response served by service worker has redirections"）。
  //   結果、オフラインで**アプリがまったく開かなくなる。**
  //   ★`vite preview` はリダイレクトしないのでローカルでは再現しない。
  //     `web/tools/preview-like-cloudflare.py` で本番の癖を真似て確かめること。
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(["./"])));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    (async () => {
      for (const k of await caches.keys()) {
        if (k.startsWith("kansui-n-") && !MINE.has(k)) await caches.delete(k);
      }
      await self.clients.claim();
    })(),
  );
});

// アプリ側から「今すぐ新版に入れ替える」と言われたときだけ交代する。
// ★勝手に skipWaiting しない。読み込み済みの区画と版がずれる可能性がある。
self.addEventListener("message", (e) => {
  if (e.data === "skip-waiting") self.skipWaiting();
});

function cacheable(res) {
  return res && res.status === 200 && res.type !== "opaque";
}

/**
 * リダイレクトを経た応答を、素の応答に作り直す。
 *
 * ★画面遷移（navigate）に `redirected: true` の応答を返すとブラウザが拒否し、
 *   「Response served by service worker has redirections」で**何も表示されない**。
 *   保存する前・返す前に必ず通すこと。
 */
async function unredirect(res) {
  if (!res || !res.redirected) return res;
  const body = await res.blob();
  return new Response(body, {
    status: res.status,
    statusText: res.statusText,
    headers: res.headers,
  });
}

async function trim(cacheName, limit, drop, keep = () => false) {
  const c = await caches.open(cacheName);
  // ★★`keep` に当たるものは数えも消しもしない。区画の間引きで **index.json まで消していた**
  //   （2026-09-23 のレビューで指摘）。オフラインで開くと「index.json が取得できません: 504」で
  //   起動できず、キャッシュ済みの区画があっても何もできなかった。
  const keys = (await c.keys()).filter((k) => !keep(k));
  if (keys.length <= limit) return;
  for (const k of keys.slice(0, drop)) await c.delete(k);
}

/** 区画の間引きで消してはいけないもの（アプリの起動に要る） */
// ★区画の上限で古い順に捨てるとき、捨てないもの: index.json と、住所検索の places.json（10.5MB）。
//   places.json が捨てられると、オフラインで住所検索が使えなくなる（7回目のレビュー）
const isIndex = (req) => {
  const p = new URL(req.url).pathname;
  return p.endsWith("/data/index.json") || p.endsWith("/data/places.json");
};

/** 先にキャッシュを返し、裏で取り直す。データの鮮度より起動の速さを優先する */
async function staleWhileRevalidate(req, cacheName) {
  const c = await caches.open(cacheName);
  const hit = await c.match(req);
  const net = fetch(req)
    .then((res) => {
      if (cacheable(res)) c.put(req, res.clone());
      return res;
    })
    .catch(() => null);
  if (hit) return hit;
  const res = await net;
  if (res) return res;
  return new Response("オフラインのため取得できません", { status: 504 });
}

/** 一度取れたら変わらないもの（内容ハッシュ付きの資産・区画・タイル）向け */
async function cacheFirst(req, cacheName, onStore) {
  const c = await caches.open(cacheName);
  const hit = await c.match(req);
  if (hit) return hit;
  let res;
  try {
    res = await fetch(req);
  } catch {
    return new Response("オフラインのため取得できません", { status: 504 });
  }
  if (cacheable(res)) {
    await c.put(req, res.clone());
    if (onStore) await onStore();
  }
  return res;
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  // 画面遷移。新しい版を優先しつつ、通信が無ければ保存済みで開く。
  // ★保存も返却も、リダイレクトを剥がしてから行う（→ `unredirect`）。
  //   ★保存先の鍵は `"./"`。`"./index.html"` を鍵にすると、308 で正規化する
  //     配信ではオフライン時にリダイレクト済みの応答を引き当ててしまう。
  if (req.mode === "navigate") {
    // ★★アプリの入口だけを `"./"` に保存する。解説ページ（guide.html）などを開いたときに
    //   `"./"` へ保存すると、**オフラインでアプリを開くと解説ページが出る**（2026-10-07 に気づいた）。
    //   入口以外は自分の URL（クエリ・ハッシュなし）を鍵にする。Cloudflare は `.html` を外して 308 するので、
    //   拡張子の有無も吸収する。
    const p = url.pathname;
    const isEntry = p === "/" || p.endsWith("/") || p.endsWith("/index.html") || p.endsWith("/index");
    const key = isEntry ? "./" : url.origin + p.replace(/\.html$/, "");
    e.respondWith(
      (async () => {
        try {
          const res = await unredirect(await fetch(req));
          if (cacheable(res)) {
            const c = await caches.open(SHELL);
            await c.put(key, res.clone());
          }
          return res;
        } catch {
          return (await caches.match(key)) ?? (await caches.match("./")) ?? Response.error();
        }
      })(),
    );
    return;
  }

  // ★配布データは別のオリジン（データ用の Pages プロジェクト）にあることがある。登録時の ?data= で受け取る
  if (url.origin === self.location.origin || url.origin === DATA_ORIGIN) {
    // ★区画ファイル。**版の中では中身が変わらない**のでキャッシュ優先。
    //   使ったものだけ溜まる＝先読みしない（F-11 の趣旨そのもの）。
    if (url.pathname.includes("/data/mesh/")) {
      return e.respondWith(cacheFirst(req, DATA, () => trim(DATA, MESH_LIMIT, MESH_TRIM, isIndex)));
    }
    // ★`index.json` は毎回作り直されるので裏で取り直す。
    //   ここをキャッシュ優先にすると、**区画だけ新しく index が古い**状態になり、
    //   `layout` の食い違いで起動時に落ちる。
    if (url.pathname.includes("/data/")) {
      return e.respondWith(staleWhileRevalidate(req, DATA));
    }
    // ★★MapLibre のワーカーは**ハッシュの付かない固定名**で出している（T-13）。
    //   中身が変わっても URL が変わらないので、キャッシュ優先にすると
    //   `SHELL_VERSION` を上げるまで**古いワーカーが residual に居座る。**
    //   → こちらは裏で取り直す。
    if (url.pathname.includes("maplibre-gl-")) {
      return e.respondWith(staleWhileRevalidate(req, SHELL));
    }
    // Vite が出す内容ハッシュ付きの資産は中身が変わらない
    if (url.pathname.includes("/assets/")) return e.respondWith(cacheFirst(req, SHELL));
    return;
  }

  if (TILE_HOSTS.has(url.hostname)) {
    e.respondWith(cacheFirst(req, TILES, () => trim(TILES, TILE_LIMIT, TILE_TRIM)));
    return;
  }
});
