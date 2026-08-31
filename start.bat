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

REM ---------------------------------------------------------------
REM Finish an interrupted update before starting.
REM update_engine.py --check-complete exits 10 when the code is newer
REM than the installed packages, 11 when it cannot tell, 0 when ready.
REM Any other code means the check itself failed, so we warn and start
REM anyway - same meaning as start.sh.
REM Keep this block flat and free of parentheses inside echo text:
REM the v0.2.29 update.bat died because "(" in an echo argument broke
REM the enclosing if-block. See
REM docs/issues/v0229_update_bat_truncates_after_git_pull.md
REM ---------------------------------------------------------------
if not exist ".venv\Scripts\python.exe" goto :update_check_done
".venv\Scripts\python.exe" "scripts\update_engine.py" --check-complete
if errorlevel 11 goto :update_check_skipped
if errorlevel 10 goto :finish_update
if errorlevel 1 goto :update_check_skipped
goto :update_check_done

:finish_update
echo.
echo [INFO] Update was interrupted; finishing it now...
".venv\Scripts\python.exe" "scripts\update_engine.py" --manual
if errorlevel 1 goto :finish_update_failed
echo [OK] Update finished.
goto :update_check_done

:finish_update_failed
echo.
echo [ERROR] The interrupted update could not be finished.
echo [ERROR] SAIVerse was NOT started, because the code and the installed
echo [ERROR] packages do not match. See self_update.log for details.
pause
exit /b 1

:update_check_skipped
echo [WARN] Could not verify the update state. Starting anyway.

:update_check_done

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

REM Build and start Frontend (production mode for stability)
echo [INFO] Building frontend...
cd frontend && call npm run build && cd ..
echo [INFO] Starting frontend...
start "SAIVerse Frontend" cmd /k "title SAIVerse Frontend && cd frontend && npm start"

REM Wait for frontend to initialize
timeout /t 3 /nobreak >nul

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
echo     [SAIVerse Frontend] - Next.js server
echo   Do NOT close them while SAIVerse is running.
echo.
echo   To stop: close all the command prompt windows.
echo.
echo   You can close THIS window.
echo.
pause
