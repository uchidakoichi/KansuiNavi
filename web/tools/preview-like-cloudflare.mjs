/**
 * `dist/` を **Cloudflare Pages と同じ癖で** 配る検証用サーバ（Node 版）。
 *
 *     node tools/preview-like-cloudflare.mjs [--host 0.0.0.0] [--port 4173]
 *
 * ★なぜ Node 版を用意したか（2026-09-21）:
 *   Python 版は `localhost` では応答するのに **LAN の IP だけ応答しなかった**。
 *   同じ機械の Node（`vite preview`）は LAN で応答していたので、
 *   macOS のローカルネットワークの許可が Python を塞いでいたと見られる。
 *   ★**実機から見るのが目的の道具**なので、通る方で書く。
 *
 * ★なぜこの道具が要るのか
 *   このプロジェクトは「本番だけ壊れる」不具合を繰り返し踏んでいる（T-13/T-14/T-15）。
 *   いずれも `vite preview` では再現しなかった。配信側の癖が違うためである。
 *   **`npm run preview` が通ったことは、本番で動く根拠にならない。**
 *
 * 再現している癖（2026-09-08 に本番で実測した挙動）:
 *   1. `/index.html` を `/` へ **308 リダイレクト**する（→ T-15）
 *   2. `.gz` に `Content-Encoding` を付けず、**生の gzip** をそのまま返す（→ T-14）
 *   3. 存在しないパスに 404 ではなく **200 ＋ index.html** を返す（→ T-14 の追記）
 *
 * ★接続は**同時に捌く**。Python 版は単一スレッドで、止まった接続が1本あるだけで
 *   **サーバ全体が応答しなくなった**（2026-09-21 に実際に踏み、
 *   ブラウザ側が「ページが固まった」ようにしか見えなかった）。
 *   オフラインや不安定な通信を試す道具なので、**止まった接続が出ることこそ前提**である。
 *
 * ★これは本番の完全な模倣ではない。`_headers` も brotli も再現していない。
 *   **最終確認は必ず実配信で行うこと。**
 */

import { createReadStream, statSync } from "node:fs";
import { createServer } from "node:http";
import { dirname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const argOf = (name, fallback) => {
  const i = process.argv.indexOf(name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
};
const HOST = argOf("--host", "127.0.0.1");
const PORT = Number(argOf("--port", "4173"));
const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "dist");

/** ★`.gz` は `application/gzip`。**`Content-Encoding` は付けない**（癖2） */
const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".mjs": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".woff2": "font/woff2",
  ".bin": "application/octet-stream",
  ".gz": "application/gzip",
};

const typeOf = (p) => {
  const i = p.lastIndexOf(".");
  return (i >= 0 && TYPES[p.slice(i)]) || "application/octet-stream";
};

const exists = (p) => {
  try {
    return statSync(p).isFile();
  } catch {
    return false;
  }
};

createServer((req, res) => {
  const path = (req.url ?? "/").split("?")[0];

  // 癖1: /index.html → / の 308
  if (path === "/index.html") {
    res.writeHead(308, { Location: "/" });
    res.end();
    return;
  }

  // ★`..` で dist の外へ出さない
  const rel = normalize(decodeURIComponent(path)).replace(/^(\.\.[/\\])+/, "").replace(/^\/+/, "");
  let file = join(ROOT, rel || "index.html");
  if (rel.endsWith("/") || !rel) file = join(ROOT, rel, "index.html");

  // 癖3: 見つからないパスは 404 ではなく 200 ＋ index.html
  if (!exists(file)) file = join(ROOT, "index.html");

  res.writeHead(200, { "content-type": typeOf(file) });
  createReadStream(file).pipe(res);
}).listen(PORT, HOST, () => {
  console.log(`Cloudflare Pages の癖を真似て ${ROOT} を配信中: http://${HOST}:${PORT}/`);
  console.log("  /index.html は 308 で / へ ／ .gz は生の gzip ／ 未知のパスは 200+index.html");
});
