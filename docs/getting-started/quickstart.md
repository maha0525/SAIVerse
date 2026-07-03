# クイックスタート

SAIVerseを10分で動かすためのガイドです。

## 前提

[インストール](./installation.md) が完了していることを前提とします。

## 起動 (推奨)

セットアップスクリプトで環境構築済みであれば、ワンクリックで起動できます。

### Windows

**`start.bat`** をダブルクリックするだけで、バックエンドとフロントエンドが起動し、ブラウザが自動で開きます。

### macOS / Linux

```bash
./start.sh
```

オプション:
- `./start.sh city_b` — 別のCityを起動
- `SAIVERSE_SEARXNG=1 ./start.sh` — SearXNGサーバーも同時起動

停止するには `Ctrl+C` を押してください。

## 手動起動

スクリプトを使わずに手動で起動する場合は、以下の手順で行います。

### 1. バックエンド（APIサーバー + マネージャー）の起動

```bash
python main.py city_a
```

起動すると以下が立ち上がります：
- バックエンド（UI + API）: `http://localhost:8000`（API は `http://localhost:8000/api`）
- City マネージャー: メモリ上で世界を管理

### 2. フロントエンドの起動

別のターミナルで：

```bash
cd frontend
npm run dev
```

フロントエンドが起動します：
- Web UI: `http://localhost:3000`

### 3. ブラウザでアクセス

http://localhost:3000 を開くと、SAIVerseのUIが表示されます。

## 最初にやること

### チュートリアル

初回起動時のチュートリアルで、**City 名 → ペルソナ作成 → モデル設定**を順に行う（後からやり直すこともできる）。

### ワールドビューを確認

左サイドバーで Building（場所）を選ぶとその場所を**閲覧**でき、チャット欄からメッセージを送るとそこに**入室して** AI が応答する（→ [ワールドビュー](../user-guide/world-view.md)）。

### 自律行動を試す

ペルソナメニュー → **ライフビュー**の再生 / 停止で、そのペルソナの自律行動を制御できる（→ [自律行動モード](../features/autonomous-mode.md)）。

### ペルソナを召喚

チャット領域ヘッダの **People 管理**から、別の Building にいるペルソナを現在地に召喚できる。

## トラブルシューティング

### ポートが使用中

`main.py` に個別ポート指定の引数はない（`city_name` / `--db-file` / `--sds-url` のみ）。別の City を起動する（`python main.py city_b` は 9000 系）か、`cities.json` のポート設定を変更する。

### APIエラーが発生

`.env` のAPIキーが正しく設定されているか確認してください。無料枠のキーでは一部機能が制限されることがあります。

## 次のステップ

- [設定](./configuration.md) - 詳細な設定オプション
- [基本概念](../concepts/README.md) - システムの仕組みを理解
- [ユーザーガイド](../user-guide/world-view.md) - UIの詳しい使い方
