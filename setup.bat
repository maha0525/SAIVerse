@echo off
chcp 65001 >nul 2>nul
setlocal enabledelayedexpansion

echo ========================================
echo   SAIVerse Setup
echo ========================================
echo.

REM --- 1. Python check ---
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python が見つかりません。
    echo   https://www.python.org/downloads/ からインストールしてください。
    echo   インストール時に "Add Python to PATH" にチェックを入れてください。
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PY_VERSION=%%v
echo [OK] %PY_VERSION%

REM --- 2. Node.js check ---
where node >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Node.js が見つかりません。
    echo   https://nodejs.org/ からインストールしてください。
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('node --version') do set NODE_VERSION=%%v
echo [OK] Node.js %NODE_VERSION%

REM --- 3. Create venv if not exists ---
if not exist ".venv" (
    echo.
    echo [SETUP] Python仮想環境を作成中...
    python -m venv .venv
    if %errorlevel% neq 0 (
        echo [ERROR] 仮想環境の作成に失敗しました。
        pause
        exit /b 1
    )
    echo [OK] .venv を作成しました
) else (
    echo [OK] .venv は既に存在します
)

REM --- 4. Activate venv ---
call .venv\Scripts\activate.bat

REM --- 5. pip install ---
echo.
echo [SETUP] Pythonパッケージをインストール中...
python -m pip install --upgrade pip >nul 2>nul
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] pip install に失敗しました。
    pause
    exit /b 1
)
echo [OK] Pythonパッケージのインストール完了

REM --- 6. npm install ---
echo.
echo [SETUP] フロントエンドパッケージをインストール中...
pushd frontend
call npm install
if %errorlevel% neq 0 (
    echo [ERROR] npm install に失敗しました。
    popd
    pause
    exit /b 1
)
popd
echo [OK] フロントエンドパッケージのインストール完了

REM --- 7. Database seed (only if not exists) ---
set SAIVERSE_DB=%USERPROFILE%\.saiverse\user_data\database\saiverse.db
if not exist "%SAIVERSE_DB%" (
    echo.
    echo [SETUP] データベースを初期化中...
    python database\seed.py --force
    if %errorlevel% neq 0 (
        echo [ERROR] データベースの初期化に失敗しました。
        pause
        exit /b 1
    )
    echo [OK] データベースの初期化完了
) else (
    echo [OK] データベースは既に存在します
)

REM --- 8. Create .env from example if not exists ---
if not exist ".env" (
    echo.
    echo [SETUP] .env ファイルを作成中...
    copy .env.example .env >nul
    echo [OK] .env を作成しました
    echo   APIキーの設定は初回起動時のチュートリアルで行えます。
) else (
    echo [OK] .env は既に存在します
)

REM --- 9. SearXNG setup ---
echo.
echo [SETUP] SearXNG (Web検索エンジン) をセットアップ中...
powershell -ExecutionPolicy Bypass -File scripts\setup_searxng.ps1
if %errorlevel% neq 0 (
    echo [WARN] SearXNG のセットアップに失敗しましたが、Web検索なしでも動作します。
) else (
    echo [OK] SearXNG のセットアップ完了
)

REM --- 10. Pre-download embedding model ---
echo.
echo [SETUP] 埋め込みモデルをダウンロード中 (初回のみ、数分かかります)...
python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-m3')"
if %errorlevel% neq 0 (
    echo [WARN] 埋め込みモデルのダウンロードに失敗しましたが、初回起動時に再試行されます。
) else (
    echo [OK] 埋め込みモデルのダウンロード完了
)

REM --- 11. Complete ---
echo.
echo ========================================
echo   セットアップ完了!
echo ========================================
echo.
echo 起動方法:
echo   start.bat をダブルクリック
echo   または以下のコマンドを実行:
echo     .venv\Scripts\activate
echo     python main.py city_a
echo     (別ターミナルで) cd frontend ^&^& npm run dev
echo.
echo ブラウザで http://localhost:3000 を開いてください。
echo.
pause
