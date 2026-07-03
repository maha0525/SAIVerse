# インストール

SAIVerseの環境構築手順を説明します。

## 前提条件

- **Python 3.12 推奨**（3.11〜3.13 で動作。3.14 以降は非対応）([ダウンロード](https://www.python.org/downloads/release/python-31210/))
  - インストール時に「Add python.exe to PATH」にチェックを入れること
- **Node.js 18以上** ([ダウンロード](https://nodejs.org/))（未導入の場合 `setup.bat` が自動導入）
- **Git**
  - **Windows は `setup.bat` が自動導入する**（winget、無理ならポータブル版）ので事前準備は不要
  - macOS / Linux は推奨（自動更新に必要）。未導入なら手動で入れる（`brew install git` / `sudo apt install git`）。`setup.sh` は Git がなくても続行するが警告を出す

## 簡単セットアップ (推奨)

セットアップスクリプトが仮想環境の作成、依存パッケージのインストール、データベース初期化、埋め込みモデルのダウンロードを自動で行います。

### ソースの入手（クローン または ZIP）

まず SAIVerse のフォルダを用意します。次のどちらでも構いません:

- **Git でクローン**（推奨・以後の自動更新が楽）:
  ```bash
  git clone https://github.com/maha0525/SAIVerse.git
  ```
  Windows で Git が未導入でも問題ありません（後述の `setup.bat` が Git を自動導入します。先に ZIP で入手してから `setup.bat` を実行しても構いません）。
- **ZIP でダウンロード**（Git 不要）: [最新版 ZIP](https://github.com/maha0525/SAIVerse/releases/latest/download/SAIVerse.zip) を任意の場所に解凍します。GitHub ページの「Code」ボタン →「Download ZIP」でも入手できます（こちらは開発中の最新版）。

### Windows

1. 入手した `SAIVerse` フォルダを開く
2. `SAIVerse` フォルダ内の **`setup.bat`** をダブルクリック
3. セットアップ完了後、**`start.bat`** をダブルクリック
4. ブラウザで http://localhost:3000 が自動的に開きます

### macOS / Linux

Git でクローンする場合:

```bash
git clone https://github.com/maha0525/SAIVerse.git
cd SAIVerse
chmod +x setup.sh start.sh
./setup.sh
./start.sh
```

ZIP で入手した場合は、解凍したフォルダに `cd` してから `chmod +x setup.sh start.sh && ./setup.sh` を実行します（macOS / Linux は Git が自動導入されないので、自動更新を使うなら別途 Git を入れておくと便利です）。

初回起動時にチュートリアルが表示され、**ユーザー名 → City 名 → ペルソナ作成 → API キー → モデル設定**を順に案内します。

## 手動セットアップ

セットアップスクリプトを使わずに手動で環境構築する場合の手順です。

### 1. ソースの入手

Git でクローン、または [最新版 ZIP](https://github.com/maha0525/SAIVerse/releases/latest/download/SAIVerse.zip) を解凍してそのフォルダに移動します。

```bash
git clone https://github.com/maha0525/SAIVerse.git
cd SAIVerse
```

### 2. Python仮想環境の作成

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate
```

### 3. Python依存パッケージのインストール

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. フロントエンドのセットアップ

```bash
cd frontend
npm install
cd ..
```

### 5. 環境変数の設定

`.env.example` をコピーして `.env` を作成します。APIキーは初回起動時のチュートリアルでも設定できます。

```bash
cp .env.example .env
```

### 6. データベースの初期化

```bash
python database/seed.py
```

これで初期のCity、Building、AIペルソナがセットアップされます。初期データは `builtin_data/seed_data.json` で定義されており、編集することでカスタマイズできます。

## 次のステップ

[クイックスタート](./quickstart.md) に進んで、SAIVerseを起動しましょう。
