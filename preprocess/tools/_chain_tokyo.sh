#!/bin/bash
# 東京都＋神奈川県の作成（2026-09-23）。
# ★道路抽出（ローカル CPU のみ）と幅員タイル取得（通信・逐次）は同時に走らせてよい。
#   ★DEM と幅員タイルは**同じサーバ**なので同時に叩かない（絶対ルール3）。
#   DEM は build_region の中で後から逐次に取る。
# ★終了コードは wait の直後に変数へ取る。`echo "... $(date) ... $?"` と書くと
#   **`$(date)` が `$?` を 0 で上書きする**（2026-09-23、失敗を「exit 0」と表示した）。
set -u
cd "$(dirname "$0")/.."
ts() { date '+%H:%M'; }
echo "=== $(ts) 開始 ==="
uv run python tools/p2_extract_roads.py > /tmp/tk_p2.log 2>&1 &
P2=$!
uv run python tools/fetch_gsi_width.py > /tmp/tk_gsi.log 2>&1 &
GSI=$!
wait $P2;  RC_P2=$?;  echo "=== $(ts) 道路抽出 終了 exit=$RC_P2 ==="
wait $GSI; RC_GSI=$?; echo "=== $(ts) 幅員タイル 終了 exit=$RC_GSI ==="
if [ $RC_P2 -ne 0 ] || [ $RC_GSI -ne 0 ]; then
  echo "★前段が失敗した。作成に進まない"; tail -5 /tmp/tk_p2.log; tail -5 /tmp/tk_gsi.log; exit 1
fi
uv run python tools/build_region.py > /tmp/tk_build.log 2>&1
RC_B=$?
echo "=== $(ts) 作成 終了 exit=$RC_B ==="
exit $RC_B
