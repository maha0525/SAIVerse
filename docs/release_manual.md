# SAIVerse β版 リリース手順書

このドキュメントは、SAIVerseプロジェクトをベータ版として一般公開するための手順をまとめたものです。

## 概要

リリース作業は、大きく分けて以下の3つのフェーズで構成されます。

1.  **インフラの準備**: 世界中のCityを繋ぐためのSAIVerse Directory Service (SDS) をインターネット上に公開します。
2.  **ドキュメントとリポジトリの準備**: 初めて触れるユーザーや開発者が迷わないように、ドキュメントを整備し、リポジトリをクリーンアップします。
3.  **公開**: GitHubリポジトリを公開し、リリースタグを作成します。

---

## フェーズ1: インフラの準備 (SDSのデプロイ)

`sds_server.py`をクラウドプラットフォームにデプロイし、24時間稼働させます。ここでは、無料枠で永続ディスクが利用可能な**Render**を推奨プラットフォームとして手順を記載します。

### Step 1.1: `requirements.txt` の更新

SDSサーバーの実行に必要なライブラリを`requirements.txt`に追加します。

```txt
# requirements.txt に以下を追記
fastapi
uvicorn
```

### Step 1.2: RenderでのWeb Service作成

1.  Renderにログインし、Dashboardから「New +」→「Web Service」を選択します。
2.  「Build and deploy from a Git repository」を選択し、SAIVerseのGitHubリポジトリを接続します。
3.  以下の項目を設定します。
    -   **Name**: `saiverse-sds` （任意）
    -   **Root Directory**: `.` (リポジトリのルート)
    -   **Environment**: `Python 3`
    -   **Region**: `(任意)`
    -   **Branch**: `main` (またはリリース用のブランチ)
    -   **Build Command**: `pip install -r requirements.txt`
    -   **Start Command**: `uvicorn sds_server:app --host 0.0.0.0 --port $PORT`
    -   **Instance Type**: `Free`

4.  「Create Web Service」をクリックしてサービスを作成します。

### Step 1.3: 永続ディスクの追加

SDSは`sds.db`というファイルにCityの情報を保存するため、サーバーが再起動してもデータが消えないように永続ディスクを設定します。

1.  作成したWeb Serviceのサイドメニューから「Disks」を選択します。
2.  「New Disk」をクリックします。
3.  以下の項目を設定します。
    -   **Name**: `sds-data` （任意）
    -   **Mount Path**: `/data`
    -   **Size (GB)**: `1`
4.  「Create Disk」をクリックします。

### Step 1.4: デプロイと動作確認

1.  手動でデプロイを実行するか、GitリポジトリにPushして自動デプロイをトリガーします。
2.  デプロイが完了すると、`https://saiverse-sds.onrender.com`のような公開URLが払い出されます。
3.  ローカルPCで、このURLを使ってSAIVerseを起動し、SDSに接続できるか確認します。
    ```bash
    python main.py city_a --sds-url https://saiverse-sds.onrender.com
    ```
4.  ログに「Successfully registered with SDS」と表示されれば成功です。

---

## フェーズ2: ドキュメントとリポジトリの準備

### Step 2.1: `README.md`の拡充

プロジェクトの「顔」となる`README.md`を、以下の内容を含むように全面的に書き直します。

-   SAIVerseとは何か（魅力的な概要）
-   主な機能（スクリーンショットを含む）
-   技術スタック
-   **セットアップ手順（最重要）**: 初心者でも迷わないように、コマンドを一つずつ丁寧に記述します。
-   基本的な使い方
-   ライセンス（MIT Licenseなど）

### Step 2.2: `.gitignore`の最終確認

`.env`ファイル、`saiverse.db`、ログファイル、`.saiverse/`ディレクトリなどがコミット対象に含まれていないことを再度確認します。

### Step 2.3: 開発者向けドキュメントの更新

-   `docs/architecture.md`: 最新のアーキテクチャ図とコンポーネント説明に更新します。
-   `docs/database_design.md`: 最新のテーブルスキーマを反映させます。

### Step 2.4: `CONTRIBUTING.md`の作成 (任意だが推奨)

他の開発者がプロジェクトに貢献しやすくなるように、バグ報告やプルリクエストの手順を記述した`CONTRIBUTING.md`を作成します。

---

## フェーズ3: GitHubでの公開

### Step 3.1: リポジトリの公開設定

GitHubリポジトリの設定画面で、リポジトリを「Public」に変更します。

### Step 3.2: リリースタグの作成

GitHubの「Releases」ページから、「Create a new release」をクリックし、`v0.1.0-beta`のようなバージョンのタグを付けてリリースノートを記述し、公開します。

---

以上で、SAIVerse β版のリリース作業は完了です。

