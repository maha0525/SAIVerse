#!/usr/bin/env bash
set -euo pipefail

echo "========================================"
echo "  SAIVerse Setup"
echo "========================================"
echo ""

# --- 1. Python check ---
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] Python3 が見つかりません。"
    echo "  macOS:  brew install python3"
    echo "  Ubuntu: sudo apt install python3 python3-venv"
    exit 1
fi
PY_VERSION=$(python3 --version)
echo "[OK] $PY_VERSION"

# --- 2. Node.js check ---
if ! command -v node &>/dev/null; then
    echo "[ERROR] Node.js が見つかりません。"
    echo "  https://nodejs.org/ からインストールしてください。"
    exit 1
fi
NODE_VERSION=$(node --version)
echo "[OK] Node.js $NODE_VERSION"

# --- 3. Create venv if not exists ---
if [ ! -d ".venv" ]; then
    echo ""
    echo "[SETUP] Python仮想環境を作成中..."
    python3 -m venv .venv
    echo "[OK] .venv を作成しました"
else
    echo "[OK] .venv は既に存在します"
fi

# --- 4. Activate venv ---
source .venv/bin/activate

# --- 5. pip install ---
echo ""
echo "[SETUP] Pythonパッケージをインストール中..."
pip install --upgrade pip >/dev/null 2>&1
pip install -r requirements.txt
echo "[OK] Pythonパッケージのインストール完了"

# --- 6. npm install ---
echo ""
echo "[SETUP] フロントエンドパッケージをインストール中..."
(cd frontend && npm install)
echo "[OK] フロントエンドパッケージのインストール完了"

# --- 7. Database seed (only if not exists) ---
SAIVERSE_DB="$HOME/.saiverse/user_data/database/saiverse.db"
SAIVERSE_DB_LEGACY="database/data/saiverse.db"
if [ ! -f "$SAIVERSE_DB" ] && [ ! -f "$SAIVERSE_DB_LEGACY" ]; then
    echo ""
    echo "[SETUP] データベースを初期化中..."
    python database/seed.py --force
    echo "[OK] データベースの初期化完了"
else
    echo "[OK] データベースは既に存在します"
fi

# --- 8. Create expansion_data directory ---
if [ ! -d "expansion_data" ]; then
    mkdir expansion_data
    echo "[OK] expansion_data を作成しました（拡張パック配置用）"
else
    echo "[OK] expansion_data は既に存在します"
fi

# --- 9. Create .env from example if not exists ---
if [ ! -f ".env" ]; then
    echo ""
    echo "[SETUP] .env ファイルを作成中..."
    cp .env.example .env
    echo "[OK] .env を作成しました"
    echo "  APIキーの設定は初回起動時のチュートリアルで行えます。"
else
    echo "[OK] .env は既に存在します"
fi

# --- 10. SearXNG setup ---
echo ""
echo "[SETUP] SearXNG (Web検索エンジン) をセットアップ中..."
if bash ./scripts/setup_searxng.sh; then
    echo "[OK] SearXNG のセットアップ完了"
else
    echo "[WARN] SearXNG のセットアップに失敗しましたが、Web検索なしでも動作します。"
fi

# --- 11. Pre-download embedding model ---
echo ""
echo "[SETUP] 埋め込みモデルをダウンロード中 (初回のみ、数分かかります)..."
python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-m3')" || {
    echo "[WARN] 埋め込みモデルのダウンロードに失敗しましたが、初回起動時に再試行されます。"
}
echo "[OK] 埋め込みモデルのダウンロード完了"

# --- 12. Complete ---
echo ""
echo "========================================"
echo "  セットアップ完了!"
echo "========================================"
echo ""
echo "起動方法:"
echo "  ./start.sh"
echo "  または以下のコマンドを実行:"
echo "    source .venv/bin/activate"
echo "    python main.py city_a"
echo "    (別ターミナルで) cd frontend && npm run dev"
echo ""
echo "ブラウザで http://localhost:3000 を開いてください。"
