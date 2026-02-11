@echo off
setlocal enabledelayedexpansion

echo ========================================
echo   SAIVerse Update
echo ========================================
echo.

REM --- 1. Check venv exists ---
if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] .venv not found. Please run setup.bat first.
    pause
    exit /b 1
)

REM --- 2. Update code ---
set CODE_UPDATED=0
where git >nul 2>nul
if %errorlevel% equ 0 (
    if exist ".git" (
        echo [UPDATE] Fetching latest code with git pull...
        git pull
        if !errorlevel! neq 0 (
            echo.
            echo [WARN] git pull failed.
            echo   If there are merge conflicts, please resolve them manually.
            echo   Press any key to continue, or Ctrl+C to abort.
            pause
        ) else (
            echo [OK] Code updated
            set CODE_UPDATED=1
        )
        goto :code_update_done
    )
)

REM git not available: offer GitHub download
echo [INFO] git is not available.
echo.
echo   Choose how to update the code:
echo     1. Download from GitHub (recommended)
echo     2. Skip (if you already updated files manually)
echo.
set /p UPDATE_CHOICE="Choice (1 or 2): "
if "!UPDATE_CHOICE!"=="1" (
    echo.
    echo [UPDATE] Downloading from GitHub...
    powershell -ExecutionPolicy Bypass -File scripts\update_from_github.ps1
    if !errorlevel! neq 0 (
        echo [ERROR] Download failed.
        echo   Please download and extract manually:
        echo   https://github.com/maha0525/SAIVerse/archive/refs/heads/main.zip
        echo.
        echo   Press any key to continue, or Ctrl+C to abort.
        pause
    ) else (
        set CODE_UPDATED=1
    )
) else (
    echo [INFO] Skipped code update.
)

:code_update_done

REM --- 3. Activate venv ---
echo.
call .venv\Scripts\activate.bat

REM --- 4. pip install ---
echo [UPDATE] Updating Python packages...
python -m pip install --upgrade pip >nul 2>nul
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] pip install failed.
    pause
    exit /b 1
)
echo [OK] Python packages updated

REM --- 5. Database migration ---
set SAIVERSE_DB=%USERPROFILE%\.saiverse\user_data\database\saiverse.db
if exist "%SAIVERSE_DB%" (
    echo.
    echo [UPDATE] Updating database schema...
    python database\migrate.py --db "%SAIVERSE_DB%"
    if !errorlevel! neq 0 (
        echo [WARN] Database update failed. Check logs for details.
    ) else (
        echo [OK] Database schema updated
    )
) else (
    echo.
    echo [INFO] Database not found. Initial setup is required.
    echo   Please run setup.bat first.
    pause
    exit /b 1
)

REM --- 6. Import playbooks ---
echo.
echo [UPDATE] Updating playbooks...
python scripts\import_all_playbooks.py --force
if %errorlevel% neq 0 (
    echo [WARN] Playbook update failed.
) else (
    echo [OK] Playbooks updated
)

REM --- 7. Frontend update ---
where node >nul 2>nul
if %errorlevel% equ 0 (
    echo.
    echo [UPDATE] Updating frontend packages...
    pushd frontend
    call npm install
    if !errorlevel! neq 0 (
        echo [WARN] npm install failed.
        popd
    ) else (
        popd
        echo [OK] Frontend packages updated
    )
) else (
    echo.
    echo [WARN] Node.js not found. Skipping frontend update.
    echo   Please install from https://nodejs.org/
)

REM --- 8. Check for new .env variables ---
echo.
if exist ".env.example" (
    if exist ".env" (
        echo [INFO] New settings may have been added to .env.example.
        echo   Please compare .env.example with your .env and add any missing entries.
    )
)

REM --- 9. Complete ---
echo.
echo ========================================
echo   Update Complete!
echo ========================================
echo.
echo To start SAIVerse:
echo   Double-click start.bat
echo.
echo Note:
echo   - Check .env.example for any new settings and add them to your .env.
echo   - If something goes wrong, backups are in %USERPROFILE%\.saiverse\user_data\database\
echo.
pause
