// 配布データを別オリジンとして手元で配る（CORS 付き）。アプリが別サイトのデータを読めるかを確かめるため。
// 使い方: node tools/serve-data.mjs [port=4174]   → http://localhost:4174/data/...
import { createServer } from "node:http";
import { createReadStream, statSync } from "node:fs";
import { join, normalize } from "node:path";
const root = new URL("../public", import.meta.url).pathname;
const port = Number(process.argv[2] ?? 4174);
const types = { ".json": "application/json", ".gz": "application/gzip", ".bin": "application/octet-stream" };
createServer((req, res) => {
  const p = normalize(decodeURIComponent(new URL(req.url, "http://x").pathname));
  const f = join(root, p);
  if (!f.startsWith(join(root, "data"))) { res.writeHead(404); return res.end(); }
  try {
    if (!statSync(f).isFile()) throw 0;
    res.writeHead(200, { "Access-Control-Allow-Origin": "*", "Content-Type": types[p.slice(p.lastIndexOf("."))] ?? "application/octet-stream" });
    createReadStream(f).pipe(res);
  } catch { res.writeHead(404, { "Access-Control-Allow-Origin": "*" }); res.end(); }
}).listen(port, () => console.log(`data on http://localhost:${port}/data/`));
