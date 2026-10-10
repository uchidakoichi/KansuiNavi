# 冠水回避ルート検索（全国版・実証実験）

大雨のときに冠水しやすい道路（周りより低い区間・アンダーパス・公表されている冠水想定箇所）を避けて、
出発地から目的地までの経路を探すウェブアプリです。経路の計算はすべて端末の中で行います。

- アプリ: https://kansui.pages.dev/
- このアプリの考え方と計算方法: [`web/public/guide.html`](web/public/guide.html)
- 利用規約・免責: [`docs/利用規約_免責.md`](docs/利用規約_免責.md) ／ プライバシーポリシー: [`docs/プライバシーポリシー_草案.md`](docs/プライバシーポリシー_草案.md)

★**実証実験のアプリです。** 国土地理院・国土交通省・各地方整備局等の公式のアプリではありません。
経路の安全は保証しません。通行の際は現地の案内や規制情報に従ってください。

神奈川県藤沢市で作った PoC（藤沢版）を全国に広げたものです。

## 構成

| ディレクトリ | 中身 |
|---|---|
| `web/` | アプリ本体（TypeScript・Vite・MapLibre GL JS）。経路探索は Web Worker（`src/router.worker.ts`） |
| `preprocess/` | 配布データを作る前処理（Python・uv）。OSM の道路網に、国土地理院の標高・幅員区分から冠水しやすさを付ける |
| `docs/` | 要件定義書の草案・設計・検証の記録 |
| `CLAUDE.md` | 開発の約束事と、これまでに踏んだ落とし穴 |

配布データ（全国 4,516 区画・約 580MB）はリポジトリに入れていません。`preprocess/` で作り、別の場所から配信しています。

## 動かし方

```bash
# アプリ（手元のデータを web/public/data/ に置いた場合）
cd web
npm ci
npm run build
npx vite preview

# 配布データの置き場を別にする場合（例）
VITE_DATA_BASE=https://<データの置き場>/data npm run build
```

```bash
# 前処理（地域ごとに道路網を作り、全国の配布データに書き出す）
cd preprocess
uv sync
uv run pytest
```

前処理の手順・所要時間・注意点は [`CLAUDE.md`](CLAUDE.md) と [`docs/StepN2_結果.md`](docs/StepN2_結果.md) にあります。
地理院タイルの取得は逐次・同時接続1・キャッシュ付きです（`preprocess/config.yaml` の `tiles`）。

## ライセンス

- **ソースコード**: [MIT License](LICENSE)
- **データ**は MIT の対象ではありません。それぞれの出典の利用条件に従ってください。

| データ | 出典・利用条件 |
|---|---|
| 道路網・道路名・住所・交差点名 | © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors（ODbL） |
| 地図・標高・道路の幅員区分 | [国土地理院](https://maps.gsi.go.jp/development/ichiran.html)（淡色地図・標高タイル・国土地理院最適化ベクトルタイル）。出典の明示で利用 |
| 都道府県名・市区町村名 | 「国土数値情報（行政区域データ）」（国土交通省）を加工して作成 |
| 冠水想定箇所（`preprocess/data/manual/`） | 国土交通省 各地方整備局・北海道開発局、内閣府 沖縄総合事務局「道路防災情報Webマップ」（公共データ利用規約 第1.0版）、関東地方整備局「道路冠水注意箇所マップ」ほか各道路管理者の公表資料を本アプリが加工したもの。★関東・山梨の座標は公表されておらず、本アプリが資料の地図から推定したもの |

本アプリが加工したデータは、国・自治体の公式な情報ではありません。
