@echo off
chcp 65001 >nul 2>nul
setlocal

echo ========================================
echo   SAIVerse Starting...
echo ========================================
echo.

REM Check venv exists
if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] .venv が見つかりません。先に setup.bat を実行してください。
    pause
    exit /b 1
)

REM Start Backend
echo [INFO] バックエンドを起動中...
start "SAIVerse Backend" /min cmd /c "call .venv\Scripts\activate.bat && python main.py city_a"

REM Wait for backend to initialize
echo [INFO] バックエンドの初期化を待機中...
timeout /t 5 /nobreak >nul

REM Start Frontend
echo [INFO] フロントエンドを起動中...
start "SAIVerse Frontend" /min cmd /c "cd frontend && npm run dev"

REM Wait for frontend to initialize
timeout /t 5 /nobreak >nul

REM Open browser
echo [INFO] ブラウザを開いています...
start http://localhost:3000

echo.
echo ========================================
echo   SAIVerse が起動しました
echo ========================================
echo.
echo   Web UI: http://localhost:3000
echo.
echo   終了するには、開いたコマンドプロンプトの
echo   ウィンドウを全て閉じてください。
echo.
echo   このウィンドウは閉じても構いません。
echo.
pause
