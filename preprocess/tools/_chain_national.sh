#!/bin/bash
# 関東の連鎖が終わったら、残りの道府県を関東に近い順に流す（2026-09-23）。
#   tools/_chain_national.sh <関東の連鎖の PID>
# ★★終了は**プロセス番号で**待つ。`pgrep -f "chain_regions.sh saitama"` で待っていたら、
#   その文字列をコマンド行に含む監視用のシェル自身に一致し、**永久に始まらなかった**。
# ★★出発点（配布に入れ済みの地域）は **index.json の regions** から取る（2026-09-24）。
#   「最終表があるか」で決めると、書き出しに失敗した地域まで出発点に入り、
#   以後の書き出しがすべて道連れで失敗する（関東の連鎖は古いスクリプトで失敗を拾わない）。
# ★関東で配布に入り損ねた県は、**先頭でもう一度作る**（古いスクリプトはやり直さない）。
set -u
cd "$(dirname "$0")/.."
WAIT_PID=$1
while kill -0 "${WAIT_PID}" 2>/dev/null; do sleep 300; done
BASE=$(uv run python -c "import json;print(','.join(json.load(open('../web/public/data/index.json'))['regions']))")
AGAIN=()
for R in saitama chiba ibaraki tochigi gunma; do
  case ",${BASE}," in *",${R},"*) ;; *) AGAIN+=("${R}") ;; esac
done
echo "配布済み: ${BASE}"
[ ${#AGAIN[@]} -gt 0 ] && echo "関東で配布に入り損ねた県（先頭で作り直す）: ${AGAIN[*]}"
export KANSUI_BASE_REGIONS=${BASE}
exec tools/chain_regions.sh ${AGAIN[@]+"${AGAIN[@]}"} \
  yamanashi shizuoka nagano niigata fukushima miyagi yamagata iwate akita aomori \
  aichi gifu toyama ishikawa fukui mie shiga kyoto osaka nara wakayama hyogo \
  okayama tottori hiroshima shimane yamaguchi kagawa tokushima ehime kochi \
  fukuoka saga nagasaki kumamoto oita miyazaki kagoshima_z01 kagoshima_z02 \
  okinawa_z15 okinawa_z16 okinawa_z17 hokkaido_z11 hokkaido_z12 hokkaido_z13
