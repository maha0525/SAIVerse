# インストール

SAIVerseの環境構築手順を説明します。

## 前提条件

- **Python 3.11以上** ([ダウンロード](https://www.python.org/downloads/))
- **Node.js 18以上** ([ダウンロード](https://nodejs.org/))
- **Git**

## 簡単セットアップ (推奨)

セットアップスクリプトが仮想環境の作成、依存パッケージのインストール、データベース初期化、埋め込みモデルのダウンロードを自動で行います。

### Windows

1. リポジトリをクローン: `git clone https://github.com/maha0525/SAIVerse.git`
2. `SAIVerse` フォルダ内の **`setup.bat`** をダブルクリック
3. セットアップ完了後、**`start.bat`** をダブルクリック
4. ブラウザで http://localhost:3000 が自動的に開きます

### macOS / Linux

```bash
git clone https://github.com/maha0525/SAIVerse.git
cd SAIVerse
chmod +x setup.sh start.sh
./setup.sh
./start.sh
```

初回起動時にチュートリアルが表示され、ユーザー名やAPIキーの設定を案内します。

## 手動セットアップ

セットアップスクリプトを使わずに手動で環境構築する場合の手順です。

### 1. リポジトリをクローン

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
