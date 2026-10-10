#!/usr/bin/env python3
"""dist/ を **Cloudflare Pages と同じ癖で** 配る検証用サーバ。

    python3 tools/preview-like-cloudflare.py [--host 0.0.0.0]   # http://localhost:4173/

★なぜこれが要るのか
  このプロジェクトは「本番だけ壊れる」不具合を3回踏んでいる（CLAUDE.md T-13/T-14/T-15）。
  いずれも `vite preview` では再現しなかった。配信側の癖が違うためである。
  **`npm run preview` が通ったことは、本番で動く根拠にならない。**

再現している癖（すべて 2026-09-08 に本番で実測した挙動）:

1. **`/index.html` を `/` へ 308 リダイレクトする。**
   → Service Worker が `addAll(["./index.html"])` するとリダイレクト済みの応答が
     キャッシュに入り、オフラインの画面遷移に返せず
     「Response served by service worker has redirections」で**何も表示されない**（T-15）。
   vite preview はリダイレクトしないので絶対に再現しない。

2. **`.gz` に `Content-Encoding` を付けず、生の gzip をそのまま返す。**
   → クライアント側で `DecompressionStream` を通す経路が走る。
   vite preview は `Content-Encoding: gzip` を付けるため、逆の経路しか通らない（T-14）。

3. **存在しないパスに 404 ではなく「200 ＋ index.html」を返す。**
   → `res.ok` では取得失敗に気づけない（T-14 の追記）。

★これは本番の完全な模倣ではない。ヘッダ（`_headers`）も brotli も再現していない。
  **最終確認は必ず実配信で行うこと。**

★全国版で特に効く点（2026-09-21）:
  癖2（`.gz` に `Content-Encoding` を付けない）は、区画ファイル
  `data/mesh/*.bin.gz` を**毎回**通る経路である。`vite preview` では
  逆の経路（ブラウザが先に展開する）しか通らないため、
  **`DecompressionStream` の側は本番でしか動かない。**
  実測の「通信量」も、この2つの配信で意味が変わる（→ T-14）。
"""

from __future__ import annotations

import functools
import http.server
import socketserver
import sys
from pathlib import Path

PORT = 4173


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (標準ライブラリの命名に合わせる)
        # 癖1: /index.html → / の 308
        if self.path == "/index.html":
            self.send_response(308)
            self.send_header("Location", "/")
            self.end_headers()
            return
        # 癖3: 見つからないパスは 200 で index.html を返す
        rel = self.path.split("?", 1)[0].lstrip("/")
        if rel and not (Path(self.directory) / rel).exists():
            self.path = "/"
        super().do_GET()

    def guess_type(self, path):
        # 癖2: .gz は application/gzip。Content-Encoding は付けない
        if str(path).endswith(".gz"):
            return "application/gzip"
        return super().guess_type(path)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def main() -> int:
    # ★実機から見るときは --host 0.0.0.0（LAN 内でのみ使うこと）
    host = "127.0.0.1"
    if "--host" in sys.argv:
        host = sys.argv[sys.argv.index("--host") + 1]
    root = Path(__file__).resolve().parent.parent / "dist"
    if not root.is_dir():
        print(f"{root} がありません。先に `npm run build` を実行してください。")
        return 1
    # ★★**必ず複数スレッドで受ける。**
    #   `socketserver.TCPServer` は**1本ずつしか捌かない**ので、
    #   止まった接続が1つあるだけで**サーバ全体が応答しなくなる**。
    #   2026-09-21 に実際に踏んだ: LISTEN はしているのに何も返さなくなり、
    #   ブラウザ側が「ページが固まった」ようにしか見えなかった。
    #   ★オフラインや不安定な通信を試す道具なので、
    #     **止まった接続が出ることこそ前提**である。
    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    socketserver.TCPServer.allow_reuse_address = True
    handler = functools.partial(Handler, directory=str(root))
    with Server((host, PORT), handler) as httpd:
        print(f"Cloudflare Pages の癖を真似て {root} を配信中: http://localhost:{PORT}/")
        print("  /index.html は 308 で / へ / .gz は生の gzip / 未知のパスは 200+index.html")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
