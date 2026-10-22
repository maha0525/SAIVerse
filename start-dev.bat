@echo off
cd /d "%~dp0"
setlocal

echo ========================================
echo   SAIVerse Starting (Dev Mode)
echo ========================================
echo.

REM Check venv exists
if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] .venv not found. Please run setup.bat first.
    pause
    exit /b 1
)

REM ---------------------------------------------------------------
REM Bring the portable Node.js to the version install_node_portable.ps1
REM pins. An update from a version without this step runs the previous
REM update engine, which never replaces .node; this start does. When .node
REM is already current this only reads its version. On failure the
REM existing .node is kept and SAIVerse starts with it. A .node.old alone
REM is a replacement that died half-way; the script puts it back first.
REM See docs/intent/dependency_management.md section 2-6.
REM ---------------------------------------------------------------
set "PORTABLE_NODE="
if exist ".node\node.exe" set "PORTABLE_NODE=1"
if exist ".node.old\node.exe" set "PORTABLE_NODE=1"
if not defined PORTABLE_NODE goto :portable_node_done
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_node_portable.ps1
if errorlevel 1 echo [WARN] Could not update the portable Node.js. Starting with the existing one.
:portable_node_done

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

REM Start Frontend (dev mode with hot reload)
echo [INFO] Starting frontend (dev mode)...
start "SAIVerse Frontend" cmd /k "title SAIVerse Frontend (Dev) && cd frontend && npm run dev"

REM Wait for frontend to initialize
timeout /t 5 /nobreak >nul

REM Open browser
echo [INFO] Opening browser...
start http://localhost:3000

echo.
echo ========================================
echo   SAIVerse is running (Dev Mode)
echo ========================================
echo.
echo   Web UI: http://localhost:3000
echo.
echo   Additional windows should be open:
echo     [SearXNG]           - Web search server (if installed)
echo     [SAIVerse Backend]  - Python server
echo     [SAIVerse Frontend] - Next.js dev server (hot reload enabled)
echo   Do NOT close them while SAIVerse is running.
echo.
echo   NOTE: Dev mode uses hot reload (HMR).
echo   This may cause page refreshes over Tailscale.
echo   Use start.bat for stable remote access.
echo.
echo   To stop: close all the command prompt windows.
echo.
echo   You can close THIS window.
echo.
pause
