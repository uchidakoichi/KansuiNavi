#!/bin/bash
# 地域を順に作り、1地域できるたびに配布データを書き出し直す（2026-09-23）。
#
#   tools/chain_regions.sh saitama chiba ibaraki tochigi gunma
#
# ★DEM と幅員タイルは**同じサーバ**（cyberjapandata）なので同時に叩かない（絶対ルール3）。
#   道路抽出（ローカルの PBF と N03 のキャッシュのみ）と幅員タイル取得は同時に走らせてよい。
#   DEM は build_region の中で後から逐次に取る。★地域どうしの作成は並べない（1本ずつ）。
# ★★2026-09-26: **次の地域の前段（道路抽出＋幅員）を、いまの地域の作成と並行して走らせる。**
#   前段はどちらも手元のファイルだけを読む（道路抽出 … PBF と N03 のキャッシュ、
#   幅員 … 手元の PMTiles）ので、作成が標高タイルを取っているあいだも通信は1本のまま。
#   ★幅員を XYZ から取る設定（gsi_vector.source: xyz）では通信が2本になるので、並行させない。
#   ★各処理の最大メモリを `/usr/bin/time -l` でログに残す（16GB の機械で重なるため）。
# ★終了コードは wait / コマンドの直後に変数へ取る（`$(date)` が `$?` を上書きする。2026-09-23 に踏んだ）。
# ★★変数の直後に日本語を書くときは必ず `${VAR}` にする（`$DONE（` が変数名の一部と読まれた）。
# ★★地域名は**コマンドごとに**渡す（`KANSUI_REGION=... cmd`）。並行で走る前段と作成が
#   同じ環境変数を export で共有すると、別の地域を作ってしまう。
# ★失敗した地域は配布に入れない。**書き出しに失敗したら、その地域を外して書き出し直す**。
#   失敗した地域は最後に1回だけやり直す（通信の一時的な失敗で県がまるごと抜けないように）。
#   やり直しは並行させない（原因がメモリの取り合いかもしれないため）。
set -u
cd "$(dirname "$0")/.."
LOG=data/raw/logs
mkdir -p "$LOG"
ts() { date '+%m-%d %H:%M'; }
# ★配布に入れる地域の出発点: 環境変数 KANSUI_BASE_REGIONS（前の連鎖で作り終えた地域）→ 無ければ config
DONE=${KANSUI_BASE_REGIONS:-$(uv run python -c "from kansui.config import Config; print(','.join(Config.load().require('national.regions')))")}
MIN_FREE=$(uv run python -c "from kansui.config import Config; print(Config.load().require('national.min_free_gb'))")
SOURCE=$(uv run python -c "from kansui.config import Config; print(Config.load().require('gsi_vector.source'))")
OVERLAP=0
[ "${SOURCE}" = "pmtiles" ] && OVERLAP=1
echo "=== $(ts) 開始: $* （配布済み: ${DONE}／次の地域の前段を並行: ${OVERLAP}）==="
# ★★止めるときは連鎖だけでなく前段も止めること（孫のプロセスは連鎖を止めても残る）:
#   pkill -f chain_regions.sh; pkill -f p2_extract_roads.py; pkill -f fetch_gsi_width.py; pkill -f build_region.py

# 前段（道路抽出＋幅員）を裏で始める。終了コードは ${LOG}/${R}_prep.rc に書く
prep_start() {
  local R=$1
  rm -f "${LOG}/${R}_prep.rc"
  (
    KANSUI_REGION=${R} /usr/bin/time -l uv run python tools/p2_extract_roads.py > "${LOG}/${R}_p2.log" 2>&1 &
    P2=$!
    KANSUI_REGION=${R} /usr/bin/time -l uv run python tools/fetch_gsi_width.py > "${LOG}/${R}_gsi.log" 2>&1 &
    GSI=$!
    wait ${P2};  RC_P2=$?
    wait ${GSI}; RC_GSI=$?
    echo "${RC_P2} ${RC_GSI}" > "${LOG}/${R}_prep.rc"
  ) &
  PREP_PID=$!
  PREP_OF=${R}
}

# 前段の終わりを待つ。成功なら 0
prep_wait() {
  local R=$1
  if [ "${PREP_OF}" != "${R}" ]; then
    echo "--- $(ts) ${R} 道路抽出＋幅員 ---"
    prep_start "${R}"
  else
    echo "--- $(ts) ${R} 道路抽出＋幅員（前の地域の作成と並行で始めていた）---"
  fi
  wait ${PREP_PID}
  PREP_OF=""
  local RC_P2 RC_GSI
  read -r RC_P2 RC_GSI < "${LOG}/${R}_prep.rc" || { RC_P2=9; RC_GSI=9; }
  echo "    $(ts) 道路抽出 exit=${RC_P2} / 幅員 exit=${RC_GSI}"
  if [ "${RC_P2}" != "0" ] || [ "${RC_GSI}" != "0" ]; then echo "    ★${R} の前段が失敗"; return 1; fi
  return 0
}

enough_disk() {
  local FREE
  FREE=$(df -g . | awk 'NR==2 {print $4}')
  if [ "${FREE}" -lt "${MIN_FREE}" ]; then
    echo "★$(ts) 空きが ${FREE}GB（下限 ${MIN_FREE}GB）。$1 は流さない"; return 1
  fi
  return 0
}

# DONE に地域を足した並び。★すでに入っている（配布済みの地域を作り直す）なら足さない。
#   足すと書き出しが同じ地域を2回読む（2026-09-27、幅員の作り直しで追加）
with_region() {
  case ",${DONE}," in *",$1,"*) echo "${DONE}" ;; *) echo "${DONE},$1" ;; esac
}

# 1地域を作って書き出す。成功なら 0。★DONE は成功したときだけ伸ばす
# 第2引数: 並行で前段を始める次の地域（無ければ空）
one() {
  local R=$1 NEXT=${2:-}
  enough_disk "${R}" || return 2
  prep_wait "${R}" || return 1
  # ★★次の地域が**配布済み**（作り直しの連鎖）なら並行させない。この地域の書き出しは
  #   配布済みの全地域の道路表を読むので、並行で走る次の地域の抽出が書き換えている
  #   edges_NEXT を、古い final/NEXT と組み合わせてしまう（6回目のレビューで指摘）
  local NEXT_DONE=0
  case ",${DONE}," in *",${NEXT},"*) NEXT_DONE=1 ;; esac
  if [ -n "${NEXT}" ] && [ ${OVERLAP} -eq 1 ] && [ ${NEXT_DONE} -eq 0 ] && enough_disk "${NEXT}"; then
    echo "    $(ts) ${NEXT} の前段を並行で始める"
    prep_start "${NEXT}"
  fi
  echo "--- $(ts) ${R} 作成（DEM・Δh・アンダーパス・幅員） ---"
  KANSUI_REGION=${R} /usr/bin/time -l uv run python tools/build_region.py > "${LOG}/${R}_build.log" 2>&1
  local RC_B=$?
  echo "    $(ts) 作成 exit=${RC_B}"
  if [ ${RC_B} -ne 0 ]; then echo "    ★${R} の作成が失敗"; return 1; fi
  # F-03: ノードの標高（★キャッシュ済みの標高タイルだけを読む。通信しない）
  KANSUI_REGION=${R} uv run python tools/node_elevation.py > "${LOG}/${R}_elev.log" 2>&1
  local RC_EL=$?
  echo "    $(ts) 標高 exit=${RC_EL}（失敗しても配布は続ける。標高プロファイルが途切れるだけ）"
  local ALL
  ALL=$(with_region "${R}")
  echo "--- $(ts) 書き出し（${ALL}） ---"
  KANSUI_REGION=${R} KANSUI_NATIONAL_REGIONS="${ALL}" /usr/bin/time -l uv run python tools/export_national.py > "${LOG}/export_${R}.log" 2>&1
  local RC_E=$?
  echo "    $(ts) 書き出し exit=${RC_E}"
  if [ ${RC_E} -ne 0 ]; then
    echo "    ★${R} を入れた書き出しが失敗（${LOG}/export_${R}.log）。${R} を外す"
    # ★書き出しは一時置き場に作ってから入れ替えるので、失敗しても配布データは前のまま
    return 1
  fi
  DONE=${ALL}
  echo "    → 配布に入った地域: ${DONE}"
  return 0
}

PREP_OF=""
PREP_PID=""
FAILED=()
ARGS=("$@")
N=${#ARGS[@]}
i=0
while [ ${i} -lt ${N} ]; do
  R=${ARGS[${i}]}
  NEXT=""
  [ $((i + 1)) -lt ${N} ] && NEXT=${ARGS[$((i + 1))]}
  one "${R}" "${NEXT}"; RC=$?
  if [ ${RC} -eq 2 ]; then
    [ -n "${PREP_OF}" ] && wait ${PREP_PID}
    echo "=== $(ts) 空き不足で止める（配布に入った地域: ${DONE}）==="; exit 3
  fi
  [ ${RC} -ne 0 ] && FAILED+=("${R}")
  i=$((i + 1))
done

if [ ${#FAILED[@]} -gt 0 ]; then
  echo "=== $(ts) 失敗した地域をもう一度だけ流す（並行なし）: ${FAILED[*]} ==="
  AGAIN=()
  for R in "${FAILED[@]}"; do
    one "${R}" ""; RC=$?
    if [ ${RC} -eq 2 ]; then break; fi
    [ ${RC} -ne 0 ] && AGAIN+=("${R}")
  done
  if [ ${#AGAIN[@]} -gt 0 ]; then echo "★$(ts) 2回とも失敗した地域: ${AGAIN[*]}"; fi
fi
echo "=== $(ts) 終了（配布に入った地域: ${DONE}）==="
