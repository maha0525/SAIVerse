@echo off
chcp 65001 >nul 2>nul
setlocal enabledelayedexpansion

echo ========================================
echo   SAIVerse Update
echo ========================================
echo.

REM --- 1. Check venv exists ---
if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] .venv が見つかりません。先に setup.bat を実行してください。
    pause
    exit /b 1
)

REM --- 2. コードの更新 ---
set CODE_UPDATED=0
where git >nul 2>nul
if %errorlevel% equ 0 (
    if exist ".git" (
        echo [UPDATE] git pull で最新のコードを取得中...
        git pull
        if !errorlevel! neq 0 (
            echo.
            echo [WARN] git pull に失敗しました。
            echo   マージの競合がある場合は手動で解決してください。
            echo   続行しますか？ (Ctrl+C で中断)
            pause
        ) else (
            echo [OK] コードの更新完了
            set CODE_UPDATED=1
        )
        goto :code_update_done
    )
)

REM git が使えない場合: GitHub から自動ダウンロードするか確認
echo [INFO] git が利用できません。
echo.
echo   コードの更新方法を選択してください:
echo     1. GitHub から自動ダウンロード (推奨)
echo     2. スキップ (既に手動でファイルを更新済みの場合)
echo.
set /p UPDATE_CHOICE="選択 (1 or 2): "
if "!UPDATE_CHOICE!"=="1" (
    echo.
    echo [UPDATE] GitHub からコードをダウンロード中...
    powershell -ExecutionPolicy Bypass -File scripts\update_from_github.ps1
    if !errorlevel! neq 0 (
        echo [ERROR] ダウンロードに失敗しました。
        echo   手動で zip をダウンロードして上書きしてください:
        echo   https://github.com/maha0525/SAIVerse/archive/refs/heads/main.zip
        echo.
        echo   続行しますか？ (Ctrl+C で中断)
        pause
    ) else (
        set CODE_UPDATED=1
    )
) else (
    echo [INFO] コードの更新をスキップしました。
)

:code_update_done

REM --- 3. Activate venv ---
echo.
call .venv\Scripts\activate.bat

REM --- 4. pip install ---
echo [UPDATE] Pythonパッケージを更新中...
python -m pip install --upgrade pip >nul 2>nul
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] pip install に失敗しました。
    pause
    exit /b 1
)
echo [OK] Pythonパッケージの更新完了

REM --- 5. Database migration ---
set SAIVERSE_DB=%USERPROFILE%\.saiverse\user_data\database\saiverse.db
if exist "%SAIVERSE_DB%" (
    echo.
    echo [UPDATE] データベーススキーマを更新中...
    python database\migrate.py --db "%SAIVERSE_DB%"
    if !errorlevel! neq 0 (
        echo [WARN] データベースの更新に失敗しました。
        echo   ログを確認してください。
    ) else (
        echo [OK] データベーススキーマの更新完了
    )
) else (
    echo.
    echo [INFO] データベースが見つかりません。初回セットアップが必要です。
    echo   setup.bat を実行してください。
    pause
    exit /b 1
)

REM --- 6. Import playbooks ---
echo.
echo [UPDATE] Playbook を更新中...
python scripts\import_all_playbooks.py --force
if %errorlevel% neq 0 (
    echo [WARN] Playbook の更新に失敗しました。
) else (
    echo [OK] Playbook の更新完了
)

REM --- 7. Frontend update ---
where node >nul 2>nul
if %errorlevel% equ 0 (
    echo.
    echo [UPDATE] フロントエンドパッケージを更新中...
    pushd frontend
    call npm install
    if !errorlevel! neq 0 (
        echo [WARN] npm install に失敗しました。
        popd
    ) else (
        popd
        echo [OK] フロントエンドパッケージの更新完了
    )
) else (
    echo.
    echo [WARN] Node.js が見つかりません。フロントエンドの更新をスキップします。
    echo   https://nodejs.org/ からインストールしてください。
)

REM --- 8. Check for new .env variables ---
echo.
if exist ".env.example" (
    if exist ".env" (
        echo [INFO] .env.example に新しい設定項目が追加されている可能性があります。
        echo   .env.example と .env を比較して、必要な項目を追加してください。
    )
)

REM --- 9. Complete ---
echo.
echo ========================================
echo   アップデート完了!
echo ========================================
echo.
echo 起動方法:
echo   start.bat をダブルクリック
echo.
echo 注意:
echo   - .env.example に新しい設定項目が追加されている場合があります。
echo     お手元の .env と比較して、必要な項目を追加してください。
echo   - 問題が発生した場合は %USERPROFILE%\.saiverse\user_data\database\ にバックアップがあります。
echo.
pause
