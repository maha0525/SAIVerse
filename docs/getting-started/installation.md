# インストール

SAIVerseの環境構築手順を説明します。

## 前提条件

- **Python 3.11以上**
- **Node.js 18以上** (フロントエンド用)
- **Git**

## 手順

### 1. リポジトリをクローン

```bash
git clone https://github.com/maha/SAIVerse.git
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

`.env.example` をコピーして `.env` を作成し、必要なAPIキーを設定します。

```bash
cp .env.example .env
```

`.env` を編集して、少なくとも1つのLLM APIキーを設定してください：

```env
# いずれか1つ以上を設定
GEMINI_API_KEY=AIzaXXXXXXXX      # 推奨
OPENAI_API_KEY=sk-XXXXXXXX
CLAUDE_API_KEY=sk-ant-XXXXXXXX
```

### 6. データベースの初期化

```bash
python database/seed.py
```

これで初期のCity、Building、AIペルソナがセットアップされます。

## 次のステップ

[クイックスタート](./quickstart.md) に進んで、SAIVerseを起動しましょう。
