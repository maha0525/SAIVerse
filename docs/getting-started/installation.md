# インストール

SAIVerseの環境構築手順を説明します。

## 前提条件

- **Python 3.13 推奨**（動作確認は 3.13 系だけ。3.11 / 3.12 は非推奨 — Python 側でこの二つの系列のインストーラーの配布が終わり、セキュリティ修正を普通の手順では入れられないため。3.14 以降は非対応）([ダウンロード](https://www.python.org/downloads/release/python-31317/) — 3.13.17)
  - インストール時に「Add python.exe to PATH」にチェックを入れること
  - `setup.bat` / `setup.sh` は Python 3.13 を名指しで探す（`setup.bat` は Windows のレジストリに記録された 3.13 のインストール先、`setup.sh` は `python3.13` コマンド）。PATH の先頭にある `python` が別の版でも 3.13 を使う。3.13 が見つからないと `.venv` に触らずに止まり、入れ方を表示する。どうしてもいまの Python で進めたい場合は環境変数 `SAIVERSE_ALLOW_NON_RECOMMENDED_PYTHON=1` を設定して実行する（動作は保証しない。[環境変数](../reference/environment-vars.md)）
  - **3.11 / 3.12 から移る場合**: Python 3.13.17 を入れ（古い Python は消さなくてよい）、SAIVerse を更新してから `setup.bat` / `setup.sh` をもう一度実行する。3.13 以外で作られた `.venv` は、古いものを `.venv.old-<日時>` に改名してから 3.13 で作り直され、パッケージが入ったら古いものは消される。SAIVerse の窓が開いていると改名できないので、先にすべて閉じておく。アドオン専用の Python 環境も、導入時の答えのまま 3.13 で作り直される。データ (`~/.saiverse`) とデータベースはそのまま
- **Node.js 24.22.1以上**（22 系なら 22.23.4 以上。どちらも 2026-10-21 のセキュリティ修正版） ([ダウンロード](https://nodejs.org/))（未導入の場合 `setup.bat` が自動導入）
- **Git**
  - **Windows は `setup.bat` が自動導入する**（winget、無理ならポータブル版）ので事前準備は不要
- **Microsoft Visual C++ 再頒布可能パッケージ**（Windows のみ。記憶の検索に使う部品が必要とする）
  - 入っていない場合は `setup.bat` が Microsoft の配布元から取って自動導入する。途中で Windows が「このアプリが変更を加えることを許可しますか」と確認するので、「はい」を選ぶ
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
2. `SAIVerse` フォルダ内の **`setup.bat`** をダブルクリック（拡張子を表示していない場合は、歯車のアイコンの「setup」です。「setup.sh」ではありません）
3. セットアップ完了後、**`start.bat`** をダブルクリック（拡張子を表示していない場合は、歯車のアイコンの「start」です。「start.sh」ではありません）
4. ブラウザで http://localhost:3000 が自動的に開きます

### macOS / Linux

Git でクローンする場合:

```bash
git clone https://github.com/maha0525/SAIVerse.git
cd SAIVerse
./setup.sh
./start.sh
```

ZIP で入手した場合は、解凍したフォルダに `cd` してから `./setup.sh` を実行します（macOS / Linux は Git が自動導入されないので、自動更新を使うなら別途 Git を入れておくと便利です）。

`permission denied` のような表示が出て実行できない場合は、`chmod +x setup.sh start.sh` を実行してから再度お試しください。解凍ソフトによっては実行権限が外れることがあります。

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

`.venv` は作ったときの Python の系列に固定されるので、Python 3.13 で作ります（`python --version` が 3.13 でなければ、3.13 の python を名指しして実行します。Mac / Linux なら `python3.13 -m venv .venv`）。

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
pip install -r requirements.lock
```

`requirements.lock` は「動くと確かめた部品の版の一覧」で、全パッケージが版ごと固定されています。`setup.bat` / `setup.sh` / `update.bat` もこのファイルから入れるので、手で入れる場合も同じものを使ってください (`requirements.txt` は開発者が意図した範囲を書く側で、そこから直接は入れません)。

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
