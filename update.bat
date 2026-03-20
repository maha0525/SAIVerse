@echo off
cd /d "%~dp0"
setlocal enabledelayedexpansion

echo ========================================
echo   SAIVerse Update
echo ========================================
echo.

REM --- Phase tracking ---
set RESULT_CODE=
set RESULT_PIP=
set RESULT_DB=
set RESULT_PLAYBOOK=
set RESULT_FRONTEND=

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
    if not exist ".git" (
        echo [UPDATE] Git found but repository not initialized. Setting up git...
        git init
        git remote add origin https://github.com/maha0525/SAIVerse.git
        git fetch origin
        git branch -M main
        git reset origin/main
        git branch --set-upstream-to=origin/main
        echo [OK] Git repository initialized
    )
    if exist ".git" (
        echo [UPDATE] Fetching latest code with git pull...
        git pull
        if !errorlevel! neq 0 (
            echo [UPDATE] git pull failed. Stashing local changes and retrying...
            git stash push --include-untracked -m "SAIVerse update auto-stash"
            if !errorlevel! neq 0 (
                set RESULT_CODE=FAILED: git stash and pull failed
                echo [ERROR] git stash failed. Please resolve manually.
                pause
                goto :code_update_done
            )
            git pull
            if !errorlevel! neq 0 (
                set RESULT_CODE=FAILED: git pull failed even after stash
                echo [ERROR] git pull failed even after stash.
                pause
                goto :code_update_done
            )
            echo [OK] Code updated (local changes stashed)
            echo [INFO] Your local changes are saved in git stash.
            echo   Run 'git stash pop' to restore them.
            set CODE_UPDATED=1
            set RESULT_CODE=OK (stashed)
        ) else (
            echo [OK] Code updated
            set CODE_UPDATED=1
            set RESULT_CODE=OK
        )
        REM Show current version after pull
        if exist "VERSION" (
            set /p CURRENT_VER=<VERSION
            echo [INFO] Current version: !CURRENT_VER!
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
        set RESULT_CODE=WARN: download failed
        echo [ERROR] Download failed.
        echo   Please download and extract manually:
        echo   https://github.com/maha0525/SAIVerse/archive/refs/heads/main.zip
        echo.
        echo   Press any key to continue, or Ctrl+C to abort.
        pause
    ) else (
        set CODE_UPDATED=1
        set RESULT_CODE=OK
    )
) else (
    echo [INFO] Skipped code update.
    set RESULT_CODE=Skipped
)

:code_update_done

REM --- 3. Activate venv ---
echo.
call .venv\Scripts\activate.bat

REM --- 4. pip install ---
echo [UPDATE] Updating Python packages...
python -m pip install --upgrade pip >nul 2>nul
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    set RESULT_PIP=FAILED
    echo [ERROR] pip install failed.
    pause
    exit /b 1
)
echo [OK] Python packages updated
set RESULT_PIP=OK

REM --- 5. Database migration ---
set SAIVERSE_DB=%USERPROFILE%\.saiverse\user_data\database\saiverse.db
if exist "%SAIVERSE_DB%" (
    echo.
    echo [UPDATE] Updating database schema...
    python database\migrate.py --db "%SAIVERSE_DB%"
    if !errorlevel! neq 0 (
        set RESULT_DB=WARN: failed
        echo [WARN] Database update failed. Check logs for details.
    ) else (
        set RESULT_DB=OK
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
    set RESULT_PLAYBOOK=WARN: failed
    echo [WARN] Playbook update failed.
) else (
    set RESULT_PLAYBOOK=OK
    echo [OK] Playbooks updated
)

REM --- 7. Frontend update ---
if exist ".node\node.exe" set "PATH=%CD%\.node;%PATH%"
where node >nul 2>nul
if %errorlevel% equ 0 (
    echo.
    echo [UPDATE] Updating frontend packages...
    pushd frontend
    call npm install
    if !errorlevel! neq 0 (
        set RESULT_FRONTEND=WARN: failed
        echo [WARN] npm install failed.
        popd
    ) else (
        popd
        set RESULT_FRONTEND=OK
        echo [OK] Frontend packages updated
    )
) else (
    echo.
    set RESULT_FRONTEND=Skipped (Node.js not found)
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

REM --- 9. Summary ---
echo.
echo ========================================
echo   Update Summary
echo ========================================
if exist "VERSION" (
    set /p CURRENT_VER=<VERSION
    echo   Version:    !CURRENT_VER!
)
echo   Code:       !RESULT_CODE!
echo   Packages:   !RESULT_PIP!
echo   Database:   !RESULT_DB!
echo   Playbooks:  !RESULT_PLAYBOOK!
echo   Frontend:   !RESULT_FRONTEND!
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
