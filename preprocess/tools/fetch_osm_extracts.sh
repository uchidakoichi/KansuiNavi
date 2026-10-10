#!/bin/bash
# Geofabrik の地方別 PBF を1本ずつ取得し、MD5 で照合する（2026-09-23）。
#   tools/fetch_osm_extracts.sh 260922 hokkaido tohoku chubu kansai chugoku shikoku kyushu
# ★日付入りのファイル名で取る（-latest は中身が日々変わり、作り直しで再現できない）。
# ★User-Agent は config.yaml の tiles.user_agent をそのまま使う（ここで別の値を作らない）。
# ★1本ずつ。並べない。
set -u
cd "$(dirname "$0")/.."
DATE=$1; shift
UA=$(uv run python -c "from kansui.config import Config; print(Config.load().require('tiles.user_agent'))")
mkdir -p data/external
for R in "$@"; do
  F=${R}-${DATE}.osm.pbf
  if [ -f "data/external/$F" ] && [ -f "data/external/$F.md5" ]; then echo "${R}: 取得済み"; continue; fi
  curl -s -f -A "$UA" -o "data/external/$F.part" "https://download.geofabrik.de/asia/japan/$F" || { echo "${R}: ★取得失敗"; continue; }
  curl -s -f -A "$UA" -o "data/external/$F.md5" "https://download.geofabrik.de/asia/japan/$F.md5" || { echo "${R}: ★MD5 取得失敗"; continue; }
  GOT=$(md5 -q "data/external/$F.part"); WANT=$(cut -d' ' -f1 "data/external/$F.md5")
  if [ "$GOT" = "$WANT" ]; then mv "data/external/$F.part" "data/external/$F"; echo "${R}: OK ($(du -h "data/external/$F" | cut -f1))"
  else echo "${R}: ★MD5 不一致（$GOT != $WANT）。捨てる"; rm -f "data/external/$F.part" "data/external/$F.md5"; fi
done
