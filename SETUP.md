# Satellite Daily Check — セットアップ（10分）

file:// で開くとブラウザのオリジンが `null` になり、Android Chrome は外部への
fetch を CORS で全部ブロックする。これがデータ取得失敗の原因。
→ GitHub Pages に置き、データは GitHub Actions が事前に取得して JSON で同梱する。
　プロキシもCORSも一切使わない構成にする。

## 1. リポジトリを作る

GitHub で新規リポジトリを作成（例: `satcheck`）。**Public** にする
（Private でも Pages は使えるが有料プランが必要な場合がある）。

## 2. ファイルを置く

```
satcheck/
├── fetch_data.py                    ← そのまま
├── docs/
│   ├── index.html                   ← そのまま
│   └── data.json                    ← 初回はActions実行で自動生成される
└── .github/
    └── workflows/
        └── update-data.yml          ← update-data.yml をこの名前・場所で
```

注意: `update-data.yml` は必ず `.github/workflows/` の下に置くこと。

## 3. Pages を有効化

リポジトリ → Settings → Pages →
- Source: **Deploy from a branch**
- Branch: **main** / フォルダ: **/docs**
- Save

数分後に `https://<ユーザー名>.github.io/satcheck/` で公開される。

## 4. 初回データ生成

リポジトリ → Actions タブ → "update satellite data" → **Run workflow** を手動実行。
1〜2分で `docs/data.json` がコミットされる。
（Actions が権限エラーになる場合: Settings → Actions → General →
　Workflow permissions を **Read and write permissions** に変更）

## 5. Android のホーム画面に追加

Chrome で `https://<ユーザー名>.github.io/satcheck/` を開く →
右上メニュー → **ホーム画面に追加**。
以後アイコンをタップするだけで判定が出る。

## 動作

- 平日 21:30 UTC（日本時間 翌朝 6:30）に Actions が自動でデータ更新。
  米国クローズ後なので当日終値が入る。
- HTML は同一オリジンの `data.json` を読むだけ。オフラインでも
  端末キャッシュ（localStorage）で直近データの判定は出る。
- 画面上部に「データ生成 ○時間前」が出る。96時間超で警告が出るので、
  Actions が止まったら気づける。

## データソースについて

`fetch_data.py` は yfinance を使用 = ローカルのバックテストと同一ソース。
判定ロジック（原資産の200日SMA、±0.5%バンド）も確定版と完全一致。
Python側で計算した SMA と HTML 側の再計算が一致することは検証済み。

## 将来の拡張（必要になったら）

`fetch_data.py` の最後に判定を追加し、`below -band` の銘柄があるときだけ
Actions から通知（メール / LINE Notify / ntfy.sh など）を飛ばせる。
ただし自動通知が必要かは、先に「執行遅延の感応度分析」で
"何日見逃すと何%痛いか" を測ってから判断すること。
