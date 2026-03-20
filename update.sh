#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================"
echo "  SAIVerse Update"
echo "========================================"
echo ""

# --- Phase tracking ---
RESULT_CODE=""
RESULT_PIP=""
RESULT_DB=""
RESULT_PLAYBOOK=""
RESULT_FRONTEND=""

# --- 1. Check venv exists ---
if [ ! -d ".venv" ] || [ ! -f ".venv/bin/activate" ]; then
    echo "[ERROR] .venv が見つかりません。先に setup.sh を実行してください。"
    exit 1
fi

# --- 2. Update code ---
CODE_UPDATED=0
if command -v git &>/dev/null && [ ! -d ".git" ]; then
    echo "[UPDATE] Git が見つかりましたがリポジトリが未初期化です。セットアップ中..."
    git init
    git remote add origin https://github.com/maha0525/SAIVerse.git
    git fetch origin
    git branch -M main
    git reset origin/main
    git branch --set-upstream-to=origin/main
    echo "[OK] Git リポジトリを初期化しました"
fi
if command -v git &>/dev/null && [ -d ".git" ]; then
    echo "[UPDATE] git pull で最新コードを取得中..."
    if git pull; then
        echo "[OK] コード更新完了"
        CODE_UPDATED=1
        RESULT_CODE="OK"
    else
        RESULT_CODE="WARN: git pull failed"
        echo ""
        echo "[WARN] git pull に失敗しました。"
        echo "  マージコンフリクトがある場合は手動で解決してください。"
        echo "  Enterキーで続行、Ctrl+C で中止します。"
        read -r
    fi
    # Show current version after pull
    if [ -f "VERSION" ]; then
        echo "[INFO] Current version: $(cat VERSION)"
    fi
else
    echo "[INFO] git が利用できません。"
    echo ""
    echo "  コードの更新方法を選んでください:"
    echo "    1. GitHub からダウンロード (推奨)"
    echo "    2. スキップ (手動でファイルを更新済みの場合)"
    echo ""
    read -rp "選択 (1 or 2): " UPDATE_CHOICE
    if [ "$UPDATE_CHOICE" = "1" ]; then
        echo ""
        echo "[UPDATE] GitHub からダウンロード中..."
        REPO="maha0525/SAIVerse"
        BRANCH="main"
        ZIP_URL="https://github.com/$REPO/archive/refs/heads/$BRANCH.zip"
        TEMP_DIR=$(mktemp -d)
        ZIP_PATH="$TEMP_DIR/saiverse.zip"

        if curl -L -o "$ZIP_PATH" "$ZIP_URL" 2>/dev/null || wget -O "$ZIP_PATH" "$ZIP_URL" 2>/dev/null; then
            echo "[OK] ダウンロード完了"
            echo "[UPDATE] ファイルを展開中..."
            unzip -qo "$ZIP_PATH" -d "$TEMP_DIR"
            EXTRACTED_DIR="$TEMP_DIR/SAIVerse-$BRANCH"
            if [ -d "$EXTRACTED_DIR" ]; then
                # Copy files, preserving user data
                rsync -a --exclude='.env' --exclude='.venv/' --exclude='node_modules/' \
                    --exclude='.node/' --exclude='expansion_data/' \
                    "$EXTRACTED_DIR/" "$SCRIPT_DIR/"
                echo "[OK] ファイル更新完了"
                CODE_UPDATED=1
                RESULT_CODE="OK"
            else
                echo "[ERROR] 展開されたディレクトリが見つかりません。"
            fi
        else
            echo "[ERROR] ダウンロードに失敗しました。"
            echo "  手動でダウンロードしてください:"
            echo "  https://github.com/$REPO/archive/refs/heads/$BRANCH.zip"
        fi
        rm -rf "$TEMP_DIR"
    else
        echo "[INFO] コード更新をスキップしました。"
        RESULT_CODE="Skipped"
    fi
fi

# --- 3. Activate venv ---
echo ""
source .venv/bin/activate

# --- 4. pip install ---
echo "[UPDATE] Python パッケージを更新中..."
python -m pip install --upgrade pip >/dev/null 2>&1
if python -m pip install -r requirements.txt; then
    echo "[OK] Python パッケージ更新完了"
    RESULT_PIP="OK"
else
    RESULT_PIP="FAILED"
    echo "[ERROR] pip install に失敗しました。"
    exit 1
fi

# --- 5. Database migration ---
SAIVERSE_DB="$HOME/.saiverse/user_data/database/saiverse.db"
if [ -f "$SAIVERSE_DB" ]; then
    echo ""
    echo "[UPDATE] データベーススキーマを更新中..."
    if python database/migrate.py --db "$SAIVERSE_DB"; then
        RESULT_DB="OK"
        echo "[OK] データベーススキーマ更新完了"
    else
        RESULT_DB="WARN: failed"
        echo "[WARN] データベース更新に失敗しました。ログを確認してください。"
    fi
else
    echo ""
    echo "[INFO] データベースが見つかりません。初期セットアップが必要です。"
    echo "  先に setup.sh を実行してください。"
    exit 1
fi

# --- 6. Import playbooks ---
echo ""
echo "[UPDATE] プレイブックを更新中..."
if python scripts/import_all_playbooks.py --force; then
    RESULT_PLAYBOOK="OK"
    echo "[OK] プレイブック更新完了"
else
    RESULT_PLAYBOOK="WARN: failed"
    echo "[WARN] プレイブック更新に失敗しました。"
fi

# --- 7. Frontend update ---
if command -v node &>/dev/null; then
    echo ""
    echo "[UPDATE] フロントエンドパッケージを更新中..."
    if (cd frontend && npm install); then
        RESULT_FRONTEND="OK"
        echo "[OK] フロントエンドパッケージ更新完了"
    else
        RESULT_FRONTEND="WARN: failed"
        echo "[WARN] npm install に失敗しました。"
    fi
else
    echo ""
    RESULT_FRONTEND="Skipped (Node.js not found)"
    echo "[WARN] Node.js が見つかりません。フロントエンドの更新をスキップします。"
    echo "  https://nodejs.org/ からインストールしてください。"
fi

# --- 8. Check for new .env variables ---
echo ""
if [ -f ".env.example" ] && [ -f ".env" ]; then
    echo "[INFO] .env.example に新しい設定が追加されている可能性があります。"
    echo "  .env.example と .env を比較して、不足する設定があれば追加してください。"
fi

# --- 9. Summary ---
echo ""
echo "========================================"
echo "  Update Summary"
echo "========================================"
if [ -f "VERSION" ]; then
    echo "  Version:    $(cat VERSION)"
fi
echo "  Code:       $RESULT_CODE"
echo "  Packages:   $RESULT_PIP"
echo "  Database:   $RESULT_DB"
echo "  Playbooks:  $RESULT_PLAYBOOK"
echo "  Frontend:   $RESULT_FRONTEND"
echo "========================================"
echo ""
echo "起動方法:"
echo "  ./start.sh"
echo ""
echo "注意:"
echo "  - .env.example に新しい設定がないか確認してください。"
echo "  - バックアップは $HOME/.saiverse/user_data/database/ にあります。"
echo ""
