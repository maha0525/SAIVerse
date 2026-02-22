@echo off
cd /d "%~dp0"
setlocal

echo ========================================
echo   SAIVerse Starting...
echo ========================================
echo.

REM Check venv exists
if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] .venv not found. Please run setup.bat first.
    pause
    exit /b 1
)

REM Add portable Node.js to PATH if exists
if exist ".node\node.exe" set "PATH=%CD%\.node;%PATH%"

REM Start SearXNG if installed
if exist "scripts\.searxng-venv\Scripts\python.exe" (
    echo [INFO] Starting SearXNG server...
    start "SearXNG" cmd /k "title SearXNG && powershell -ExecutionPolicy Bypass -File scripts\run_searxng_server.ps1"
    timeout /t 3 /nobreak >nul
) else (
    echo [INFO] SearXNG not installed, skipping. Run setup.bat to install.
)

REM Start Backend
echo [INFO] Starting backend...
start "SAIVerse Backend" cmd /k "title SAIVerse Backend && call .venv\Scripts\activate.bat && python main.py city_a"

REM Wait for backend to initialize
echo [INFO] Waiting for backend to initialize...
timeout /t 5 /nobreak >nul

REM Start Frontend
echo [INFO] Starting frontend...
start "SAIVerse Frontend" cmd /k "title SAIVerse Frontend && cd frontend && npm run dev"

REM Wait for frontend to initialize
timeout /t 5 /nobreak >nul

REM Open browser
echo [INFO] Opening browser...
start http://localhost:3000

echo.
echo ========================================
echo   SAIVerse is running
echo ========================================
echo.
echo   Web UI: http://localhost:3000
echo.
echo   Additional windows should be open:
echo     [SearXNG]           - Web search server (if installed)
echo     [SAIVerse Backend]  - Python server
echo     [SAIVerse Frontend] - Next.js dev server
echo   Do NOT close them while SAIVerse is running.
echo.
echo   To stop: close all the command prompt windows.
echo.
echo   You can close THIS window.
echo.
pause
