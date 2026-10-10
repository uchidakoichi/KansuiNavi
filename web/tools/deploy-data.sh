#!/bin/bash
# 配布データ（web/public/data）を、データ用の Cloudflare Pages プロジェクトへ手動でデプロイする（2026-10-06）。
# ★アプリ本体は GitHub の master へのプッシュで自動デプロイされる。データはここから手で出す（git に入れていないため）。
# ★データを差し替えたら web/public/sw.js の DATA_VERSION を上げ、アプリ側もプッシュすること。
#
# 使い方（web/ で）:
#   KANSUI_DATA_PROJECT=<データのプロジェクト名> tools/deploy-data.sh
set -euo pipefail
cd "$(dirname "$0")/.."
: "${KANSUI_DATA_PROJECT:?データのプロジェクト名を KANSUI_DATA_PROJECT に入れること}"
BRANCH="${KANSUI_DATA_BRANCH:-master}"   # ★本番ブランチ名を確かめること（藤沢版 T-17: 違っても「成功」と出る）
[ -f public/data/index.json ] || { echo "★public/data/index.json が無い（先に preprocess で書き出す）"; exit 1; }
python3 -c "import json;json.load(open('public/data/road_kansui.json'),parse_constant=lambda c:1/0)" || { echo "★road_kansui.json が正しい JSON でない"; exit 1; }
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
# ★コピーせずハードリンクで並べる（約590MB を複製しない）
cp -al public/data "$STAGE/data" 2>/dev/null || cp -R public/data "$STAGE/data"
cp tools/data-site/_headers tools/data-site/robots.txt "$STAGE/"
echo "ファイル数: $(find "$STAGE" -type f | wc -l)（上限 20,000）"
npx wrangler pages deploy "$STAGE" --project-name="$KANSUI_DATA_PROJECT" --branch="$BRANCH"
echo "★確かめる: curl -sI https://$KANSUI_DATA_PROJECT.pages.dev/data/index.json | grep -i access-control"
