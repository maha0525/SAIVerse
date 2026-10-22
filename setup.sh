#!/usr/bin/env bash
set -euo pipefail

echo "========================================"
echo "  SAIVerse Setup"
echo "========================================"
echo ""

# --- 1. Python check ---
# SAIVerse の動作確認は Python 3.13 系だけで行っている。.venv は作ったときの Python の
# 系列に固定されるので、PATH の python3 ではなく python3.13 を名指しして使う。3.13 が
# 無ければ .venv に触らずに止める。SAIVERSE_ALLOW_NON_RECOMMENDED_PYTHON=1 のときは、
# 3.13 が無ければ PATH の python3 で進める。PYTHON_BIN は選んだ python の場所で、
# .venv を有効にする前の処理はこれを使う。setup.bat はレジストリで 3.13 を探す。
# 設計: docs/intent/dependency_management.md §2-7。
ALLOW_OTHER_PYTHON=false
if [ "${SAIVERSE_ALLOW_NON_RECOMMENDED_PYTHON:-}" = "1" ]; then
    ALLOW_OTHER_PYTHON=true
fi
PYTHON_BIN=""
if command -v python3.13 &>/dev/null \
    && python3.13 -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)' &>/dev/null; then
    PYTHON_BIN="$(command -v python3.13)"
elif [ "$ALLOW_OTHER_PYTHON" = true ]; then
    if ! command -v python3 &>/dev/null; then
        echo "[ERROR] Python3 が見つかりません。 / Python3 was not found."
        echo "  Python 3.13.17: https://www.python.org/downloads/release/python-31317/"
        exit 1
    fi
    PYTHON_BIN="$(command -v python3)"
    echo ""
    echo "[WARN] Python 3.13 が見つからないため、推奨外の Python で進めます (SAIVERSE_ALLOW_NON_RECOMMENDED_PYTHON=1)。"
    echo "       動作は保証しません。推奨の Python 3.13.17 はこちらです:"
    echo "         https://www.python.org/downloads/release/python-31317/"
    echo "       この設定のあいだは、既存の .venv をその Python の系列を理由に作り直しません。"
    echo "[WARN] Python 3.13 was not found; continuing with a Python that is not recommended"
    echo "       (SAIVERSE_ALLOW_NON_RECOMMENDED_PYTHON=1). SAIVerse is not guaranteed to work."
    echo "       The recommended Python 3.13.17 is here:"
    echo "         https://www.python.org/downloads/release/python-31317/"
    echo "       While this is set, setup does not rebuild an existing .venv because of its Python series."
    echo ""
else
    cat <<'EOF'
[ERROR] Python 3.13 (python3.13 コマンド) が見つかりません。 / Python 3.13 (the python3.13 command) was not found.

SAIVerse の動作確認は Python 3.13 系だけで行っています (3.11 / 3.12 は非推奨、3.14 以降は非対応です)。
setup はここで止まりました。.venv には何も変更していません。

1. Python 3.13.17 を入れてください。
     https://www.python.org/downloads/release/python-31317/
   macOS はページを下にスクロールして「macOS installer」をダウンロードします。
   Linux はディストリビューションのパッケージなどで python3.13 を入れてください
   (Debian / Ubuntu では python3.13-venv も要ります)。
   3.12 や 3.14 が入っていても、消さずに並べて入れられます。
   入れ終わったら、./setup.sh をもう一度実行してください。

2. いまの Python のままで進めたい場合は、環境変数 SAIVERSE_ALLOW_NON_RECOMMENDED_PYTHON=1 を
   設定してから setup をもう一度実行してください。ただし、その場合の動作は保証しません。
     SAIVERSE_ALLOW_NON_RECOMMENDED_PYTHON=1 ./setup.sh

----------------------------------------------------------------

SAIVerse is tested on Python 3.13 only (3.11 / 3.12 are not recommended,
3.14 and later are not supported). Setup stopped here. Nothing in .venv was changed.

1. Install Python 3.13.17:
     https://www.python.org/downloads/release/python-31317/
   On macOS, scroll down the page and download "macOS installer".
   On Linux, install python3.13 from your distribution's packages or similar
   (Debian / Ubuntu also need python3.13-venv).
   You can install it next to Python 3.12 or 3.14; you do not have to remove them.
   Then run ./setup.sh again.

2. To continue with the Python you have now, set the environment variable
   SAIVERSE_ALLOW_NON_RECOMMENDED_PYTHON=1 and run setup again. SAIVerse is not
   guaranteed to work in that case:
     SAIVERSE_ALLOW_NON_RECOMMENDED_PYTHON=1 ./setup.sh
EOF
    exit 1
fi
PY_VERSION=$("$PYTHON_BIN" --version)
echo "[OK] $PY_VERSION ($PYTHON_BIN)"

# --- 2. Node.js check ---
if ! command -v node &>/dev/null; then
    echo "[ERROR] Node.js が見つかりません。"
    echo "  https://nodejs.org/ からインストールしてください。"
    exit 1
fi
NODE_VERSION=$(node --version)
echo "[OK] Node.js $NODE_VERSION"

# --- 3. Create or rebuild .venv ---
# scripts/setup_venv.py prepare を、上で選んだ python で実行する。結果は一行で
# keep / create / renamed <古い名前> / locked。3.13 以外の系列で作られた .venv と
# 壊れた .venv は消さずに .venv.old-<日時> へ改名し、新しい .venv にパッケージが
# 入ってから消す。新しい .venv が失敗したら古いものを戻す。
# SAIVERSE_ALLOW_NON_RECOMMENDED_PYTHON=1 のときは既存の .venv の系列を見ない。
# setup.bat と同じ手順。
if [ "$ALLOW_OTHER_PYTHON" = true ]; then
    echo "[INFO] SAIVERSE_ALLOW_NON_RECOMMENDED_PYTHON=1: 既存の .venv の Python の系列は確かめません。"
    VENV_RESULT="$("$PYTHON_BIN" scripts/setup_venv.py prepare)" || VENV_RESULT=""
else
    VENV_RESULT="$("$PYTHON_BIN" scripts/setup_venv.py prepare --expect-series 3.13)" || VENV_RESULT=""
fi
VENV_ACTION="${VENV_RESULT%% *}"
VENV_OLD=""
if [ "$VENV_ACTION" = "renamed" ]; then
    VENV_OLD="${VENV_RESULT#renamed }"
fi

restore_old_venv() {
    if [ -n "$VENV_OLD" ]; then
        "$PYTHON_BIN" scripts/setup_venv.py rollback "$VENV_OLD" || true
    fi
}

case "$VENV_ACTION" in
    keep)
        echo "[OK] .venv は既に存在します"
        ;;
    create|renamed)
        echo ""
        echo "[SETUP] Python仮想環境を作成中..."
        if ! "$PYTHON_BIN" -m venv .venv || [ ! -f ".venv/bin/activate" ]; then
            echo "[ERROR] Python仮想環境を作れませんでした。 / Failed to create the virtual environment."
            echo "  Debian / Ubuntu では python3.13-venv (推奨外の Python で進めている場合は python3-venv) が要ります。"
            echo "  Debian / Ubuntu need python3.13-venv (python3-venv when you continue with another Python)."
            if [ -n "$VENV_OLD" ]; then
                restore_old_venv
            else
                rm -rf .venv
            fi
            exit 1
        fi
        echo "[OK] .venv を作成しました"
        ;;
    locked)
        exit 1
        ;;
    *)
        echo "[ERROR] 既存の .venv を確かめられませんでした。上のメッセージを見てください。"
        echo "[ERROR] Could not check the existing .venv folder. See the messages above."
        exit 1
        ;;
esac

# --- 4. Activate venv ---
source .venv/bin/activate

# --- 5. pip install ---
echo ""
echo "[SETUP] Pythonパッケージをインストール中..."
# pip 自身の更新の失敗では止めない (setup.bat と同じ)。set -e のまま失敗すると、
# 下の restore_old_venv に届かず、パッケージの無い .venv が残る。
python -m pip install --upgrade pip >/dev/null 2>&1 || echo "[WARN] pip 自身の更新に失敗しました。今の pip のまま続けます。 / Could not upgrade pip; continuing with the current pip."
if ! python -m pip install -r requirements.lock; then
    echo "[ERROR] Pythonパッケージのインストールに失敗しました。 / pip install failed."
    restore_old_venv
    exit 1
fi
echo "[OK] Pythonパッケージのインストール完了"

# --- 5b. Delete the old .venv and rebuild addon environments ---
# 新しい .venv が揃ったので .venv.old-<日時> を消す。続けて、別の Python の系列で
# 作られたアドオン専用の環境を、保存された答えのまま作り直す。作り直しの失敗では
# setup を止めない (SAIVerse 本体は動く)。スクリプトがどうすればよいかを表示し、
# 失敗を .addon_env_rebuild_failures.json に書き残す。
"$PYTHON_BIN" scripts/setup_venv.py finish || true
if ! python scripts/rebuild_addon_envs.py; then
    echo "[WARN] アドオンの専用の環境を確かめられませんでした。SAIVerse 本体は動きます。上のメッセージを見てください。"
fi

# --- 6. npm install ---
echo ""
echo "[SETUP] フロントエンドパッケージをインストール中..."
(cd frontend && npm install)
echo "[OK] フロントエンドパッケージのインストール完了"

# --- 7. Database seed (only if not exists or core tables missing) ---
SAIVERSE_DB="$HOME/.saiverse/user_data/database/saiverse.db"
SAIVERSE_DB_LEGACY="database/data/saiverse.db"
NEED_SEED=true

if [ -f "$SAIVERSE_DB" ]; then
    if python -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('SELECT 1 FROM city LIMIT 1'); c.close()" "$SAIVERSE_DB" 2>/dev/null; then
        NEED_SEED=false
    fi
fi
if [ "$NEED_SEED" = true ] && [ -f "$SAIVERSE_DB_LEGACY" ]; then
    if python -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('SELECT 1 FROM city LIMIT 1'); c.close()" "$SAIVERSE_DB_LEGACY" 2>/dev/null; then
        NEED_SEED=false
    fi
fi

if [ "$NEED_SEED" = true ]; then
    if [ -f "$SAIVERSE_DB" ]; then
        echo ""
        echo "[WARN] データベースファイルは存在しますが、コアテーブルがありません。再初期化します..."
    fi
    echo ""
    echo "[SETUP] データベースを初期化中..."
    python database/seed.py --force
    echo "[OK] データベースの初期化完了"
else
    echo "[OK] データベースは既に存在します"
fi

# --- 7b. Import/update playbooks ---
echo ""
echo "[SETUP] Playbook を更新中..."
if python scripts/import_all_playbooks.py --force; then
    echo "[OK] Playbook の更新完了"
else
    echo "[WARN] Playbook の更新に失敗しました。後で再実行できます: python scripts/import_all_playbooks.py --force"
fi

# --- 8. Create expansion_data directory ---
if [ ! -d "expansion_data" ]; then
    mkdir expansion_data
    echo "[OK] expansion_data を作成しました（拡張パック配置用）"
else
    echo "[OK] expansion_data は既に存在します"
fi

# --- 9. Initialize git repository if git is available ---
if command -v git &>/dev/null; then
    # scripts/init_git_repo.py は、Git の記録を「いつも最新の版」ではなく、このフォルダの
    # 中身と同じ版 (VERSION と同じ名前のタグ) に合わせる。ファイルと記録が一致しないときは
    # その場で知らせる。メッセージはスクリプトが出し、警告で終わってもセットアップは続ける。
    # setup.bat も同じスクリプトを呼ぶ。
    python scripts/init_git_repo.py || true
else
    echo ""
    echo "[WARN] Git が見つかりません。自動更新には Git のインストールが必要です:"
    if [[ "${OSTYPE:-}" == "darwin"* ]]; then
        echo "  macOS:         xcode-select --install"
        echo "                 (または brew install git)"
    elif command -v apt-get &>/dev/null; then
        echo "  Ubuntu/Debian: sudo apt install git"
    elif command -v dnf &>/dev/null; then
        echo "  Fedora/RHEL:   sudo dnf install git"
    elif command -v pacman &>/dev/null; then
        echo "  Arch Linux:    sudo pacman -S git"
    else
        echo "  https://git-scm.com/ からインストールしてください。"
    fi
    echo "  Git をインストール後、setup.sh を再実行してください。"
fi

# --- 10. Create .env from example if not exists ---
if [ ! -f ".env" ]; then
    echo ""
    echo "[SETUP] .env ファイルを作成中..."
    cp .env.example .env
    chmod 600 .env
    echo "[OK] .env を作成しました (権限: 600)"
    echo "  APIキーの設定は初回起動時のチュートリアルで行えます。"
else
    # 既存の .env も安全な権限に修正
    chmod 600 .env
    echo "[OK] .env は既に存在します"
fi

# --- 11. SearXNG setup ---
echo ""
echo "[SETUP] SearXNG (Web検索エンジン) をセットアップ中..."
if bash ./scripts/setup_searxng.sh; then
    echo "[OK] SearXNG のセットアップ完了"
else
    echo "[WARN] SearXNG のセットアップに失敗しましたが、Web検索なしでも動作します。"
fi

# --- 12. Pre-download embedding model ---
echo ""
echo "[SETUP] 埋め込みモデルをダウンロード中 (初回のみ、数分かかります)..."
# 初回起動時と同じダウンロード (sai_memory/config.py の固定ファイル一式) をして、試しに読み込む。
if python -c "from sai_memory.memory.recall import Embedder; Embedder()"; then
    echo "[OK] 埋め込みモデルのダウンロード完了"
else
    echo "[WARN] 埋め込みモデルのダウンロードに失敗しましたが、初回起動時に再試行されます。"
fi

# --- 13. Complete ---
echo ""
echo "========================================"
echo "  セットアップ完了!"
echo "========================================"
if [ -f "VERSION" ]; then
    echo "  Version: $(cat VERSION)"
fi
echo ""
echo "起動方法:"
echo "  ./start.sh"
echo "  または以下のコマンドを実行:"
echo "    source .venv/bin/activate"
echo "    python main.py city_a"
echo "    (別ターミナルで) cd frontend && npm run dev"
echo ""
echo "ブラウザで http://localhost:3000 を開いてください。"
