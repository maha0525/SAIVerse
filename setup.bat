@echo off
setlocal enabledelayedexpansion

echo ========================================
echo   SAIVerse Setup
echo ========================================
echo.

REM --- 1. Python check ---
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python not found.
    echo   Please install from https://www.python.org/downloads/
    echo   Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PY_VERSION=%%v
echo [OK] %PY_VERSION%

REM --- 2. Node.js check & auto-install ---
where node >nul 2>nul
if %errorlevel% equ 0 goto :node_found
echo.
echo [SETUP] Node.js not found. Attempting auto-install...
where winget >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Could not auto-install Node.js (winget not available).
    echo   Please install manually from https://nodejs.org/
    pause
    exit /b 1
)
winget install OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements
if %errorlevel% neq 0 (
    echo [ERROR] Node.js auto-install failed.
    echo   Please install manually from https://nodejs.org/
    pause
    exit /b 1
)
REM Add default install path to current session
set "PATH=%PATH%;C:\Program Files\nodejs"
where node >nul 2>nul
if %errorlevel% neq 0 (
    echo [OK] Node.js installed.
    echo   Please close this window and run setup.bat again to refresh PATH.
    pause
    exit /b 0
)
for /f "tokens=*" %%v in ('node --version') do set NODE_VERSION=%%v
echo [OK] Node.js %NODE_VERSION% installed
goto :node_done

:node_found
for /f "tokens=*" %%v in ('node --version') do set NODE_VERSION=%%v
echo [OK] Node.js %NODE_VERSION%

:node_done

REM --- 3. Create venv if not exists ---
if not exist ".venv" (
    echo.
    echo [SETUP] Creating Python virtual environment...
    python -m venv .venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [OK] Created .venv
) else (
    echo [OK] .venv already exists
)

REM --- 4. Activate venv ---
call .venv\Scripts\activate.bat

REM --- 5. pip install ---
echo.
echo [SETUP] Installing Python packages...
python -m pip install --upgrade pip >nul 2>nul
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] pip install failed.
    pause
    exit /b 1
)
echo [OK] Python packages installed

REM --- 6. npm install ---
echo.
echo [SETUP] Installing frontend packages...
pushd frontend
call npm install
if %errorlevel% neq 0 (
    echo [ERROR] npm install failed.
    popd
    pause
    exit /b 1
)
popd
echo [OK] Frontend packages installed

REM --- 7. Database seed (only if not exists) ---
set SAIVERSE_DB=%USERPROFILE%\.saiverse\user_data\database\saiverse.db
if not exist "%SAIVERSE_DB%" (
    echo.
    echo [SETUP] Initializing database...
    python database\seed.py --force
    if %errorlevel% neq 0 (
        echo [ERROR] Database initialization failed.
        pause
        exit /b 1
    )
    echo [OK] Database initialized
) else (
    echo [OK] Database already exists
)

REM --- 8. Create expansion_data directory ---
if not exist "expansion_data" (
    mkdir expansion_data
    echo [OK] Created expansion_data directory
) else (
    echo [OK] expansion_data already exists
)

REM --- 9. Create .env from example if not exists ---
if not exist ".env" (
    echo.
    echo [SETUP] Creating .env file...
    copy .env.example .env >nul
    echo [OK] Created .env
    echo   API keys can be configured in the first-run tutorial.
) else (
    echo [OK] .env already exists
)

REM --- 10. SearXNG setup ---
echo.
echo [SETUP] Setting up SearXNG (web search engine)...
powershell -ExecutionPolicy Bypass -File scripts\setup_searxng.ps1
if %errorlevel% neq 0 (
    echo [WARN] SearXNG setup failed. Web search will be unavailable, but the app still works.
) else (
    echo [OK] SearXNG setup complete
)

REM --- 11. Pre-download embedding model ---
echo.
echo [SETUP] Downloading embedding model (first time only, may take a few minutes)...
python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-m3')"
if %errorlevel% neq 0 (
    echo [WARN] Embedding model download failed. It will retry on first launch.
) else (
    echo [OK] Embedding model downloaded
)

REM --- 12. Complete ---
echo.
echo ========================================
echo   Setup Complete!
echo ========================================
echo.
echo To start SAIVerse:
echo   Double-click start.bat
echo   Or run these commands:
echo     .venv\Scripts\activate
echo     python main.py city_a
echo     (in another terminal) cd frontend ^&^& npm run dev
echo.
echo Then open http://localhost:3000 in your browser.
echo.
pause
