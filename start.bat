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
REM A portable Node.js replacement that died between its two renames
REM leaves .node.old and no .node. Put it back before anything uses
REM .node. The replacement itself runs during updates only - see
REM docs/intent/dependency_management.md section 2-6.
REM ---------------------------------------------------------------
if exist ".node\" goto :portable_node_ready
if not exist ".node.old\node.exe" goto :portable_node_ready
echo [INFO] Putting back the portable Node.js left aside by an interrupted replacement...
ren ".node.old" ".node"
if errorlevel 1 echo [WARN] Could not rename .node.old back to .node.
:portable_node_ready

REM ---------------------------------------------------------------
REM Finish an interrupted update before starting.
REM update_engine.py --check-complete exits 10 when the code is newer
REM than the installed packages, 11 when it cannot tell, 12 when it had
REM to install the packages once more and that failed, 0 when ready.
REM On 12 SAIVerse starts anyway and its screen shows the failure.
REM Any other code means the check itself failed, so we warn and start
REM anyway - same meaning as start.sh.
REM Keep this block flat and free of parentheses inside echo text:
REM the v0.2.29 update.bat died because "(" in an echo argument broke
REM the enclosing if-block. See
REM docs/issues/v0229_update_bat_truncates_after_git_pull.md
REM ---------------------------------------------------------------
if not exist ".venv\Scripts\python.exe" goto :update_check_done
".venv\Scripts\python.exe" "scripts\update_engine.py" --check-complete
if errorlevel 13 goto :update_check_skipped
if errorlevel 12 goto :dependencies_failed
if errorlevel 11 goto :update_check_skipped
if errorlevel 10 goto :finish_update
if errorlevel 1 goto :update_check_skipped
goto :update_check_done

:dependencies_failed
echo.
echo [WARN] The packages for this version could not be installed once more.
echo [WARN] Starting anyway. The SAIVerse screen shows what happened and what to do.
echo [WARN] If the SAIVerse screen does not open, its packages are missing too: check the
echo [WARN] internet connection, close every SAIVerse window, then run start.bat again.
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

REM Start SearXNG if installed. A SearXNG setup that stopped part-way
REM leaves .setup_in_progress\searxng-venv.json, and the SearXNG venv may
REM have no python left; SearXNG is started then too, and
REM run_searxng_server.ps1 finishes that setup with the python of .venv
REM first. See docs/intent/dependency_management.md section 2-7.
set "SEARXNG_START="
if exist "scripts\.searxng-venv\Scripts\python.exe" set "SEARXNG_START=1"
if exist ".setup_in_progress\searxng-venv.json" set "SEARXNG_START=1"
if not defined SEARXNG_START goto :searxng_not_installed
if exist ".setup_in_progress\searxng-venv.json" echo [INFO] A SearXNG setup stopped before it finished; the SearXNG window finishes it first.
echo [INFO] Starting SearXNG server...
start "SearXNG" cmd /k "title SearXNG && powershell -ExecutionPolicy Bypass -File scripts\run_searxng_server.ps1"
timeout /t 3 /nobreak >nul
goto :searxng_done
:searxng_not_installed
echo [INFO] SearXNG not installed, skipping. Run setup.bat to install.
:searxng_done

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

REM The frontend listens on PORT when it is set (next start / next dev read it),
REM else on 3000: open the browser on the same port.
set "UI_PORT=3000"
if defined PORT set "UI_PORT=%PORT%"

REM Open browser
echo [INFO] Opening browser...
start http://localhost:%UI_PORT%

echo.
echo ========================================
echo   SAIVerse is running
echo ========================================
echo.
echo   Web UI: http://localhost:%UI_PORT%
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
