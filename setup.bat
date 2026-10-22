@echo off
cd /d "%~dp0"
setlocal enabledelayedexpansion

echo ========================================
echo   SAIVerse Setup
echo ========================================
echo.

REM --- 1. Python check ---
REM SAIVerse is tested on Python 3.13 only. .venv stays on the Python series it
REM was created with, so setup names Python 3.13 instead of taking whichever
REM "python" comes first on PATH: it reads the folder that the python.org
REM installer and miniconda record under
REM Software\Python\PythonCore\3.13\InstallPath, current user first, and uses
REM python.exe in that folder. Without a 3.13, setup stops before touching .venv
REM unless SAIVERSE_ALLOW_NON_RECOMMENDED_PYTHON=1, which lets it continue with
REM the PATH python. PYTHON_EXE is the full path of the chosen python; every
REM step before .venv is activated uses it. setup.sh does the same with
REM python3.13. See docs/intent/dependency_management.md section 2-7.
REM Keep this section flat: no echo text with parentheses inside a block.
set "PYTHON_EXE="
set "ALLOW_OTHER_PYTHON="
if "%SAIVERSE_ALLOW_NON_RECOMMENDED_PYTHON%"=="1" set "ALLOW_OTHER_PYTHON=1"
call :find_python313 HKCU
if not defined PYTHON_EXE call :find_python313 HKLM
if defined PYTHON_EXE goto :python_selected
if defined ALLOW_OTHER_PYTHON goto :python_from_path
echo.
call :show_message python313_missing
pause
exit /b 1

:python_from_path
where python >nul 2>nul
if %errorlevel% equ 0 goto :python_on_path
echo [ERROR] Python not found.
echo   Please install Python 3.13.17 from https://www.python.org/downloads/release/python-31317/
echo   Make sure to check "Add python.exe to PATH" during installation.
echo.
echo   If you already installed Python, Windows App Execution Aliases
echo   may be intercepting the command. To fix:
echo     Settings ^> Apps ^> App Execution Aliases
echo     Turn off "python.exe" and "python3.exe"
pause
exit /b 1

:python_on_path
set "PY_VERSION="
for /f "tokens=*" %%v in ('python --version 2^>^&1') do set "PY_VERSION=%%v"
echo %PY_VERSION% | findstr /r "[0-9]" >nul 2>nul
if %errorlevel% equ 0 goto :python_on_path_works
echo [ERROR] Python found but version number is missing.
echo   Output was: "%PY_VERSION%"
echo.
echo   This usually means Windows App Execution Aliases are intercepting
echo   the "python" command instead of a real Python installation.
echo.
echo   To fix:
echo     1. Settings ^> Apps ^> Advanced app settings ^> App Execution Aliases
echo     2. Turn off "python.exe" and "python3.exe"
echo     3. Install Python 3.13.17 from https://www.python.org/downloads/release/python-31317/
echo        Make sure to check "Add python.exe to PATH" during installation.
echo     4. Run setup.bat again.
pause
exit /b 1

:python_on_path_works
REM The full path, because "python" means the .venv python once it is activated.
for /f "delims=" %%p in ('python -c "import sys; print(sys.executable)"') do set "PYTHON_EXE=%%p"
if defined PYTHON_EXE goto :python_warn_not_recommended
echo [ERROR] Could not find where the PATH python is installed.
pause
exit /b 1

:python_warn_not_recommended
echo.
call :show_message python_not_recommended
echo.

:python_selected
set "PY_VERSION="
for /f "tokens=*" %%v in ('call "%PYTHON_EXE%" --version 2^>^&1') do set "PY_VERSION=%%v"
echo [OK] %PY_VERSION% - %PYTHON_EXE%

REM --- 1b. Stop while SAIVerse runs ---
REM Before anything is changed: .venv, the portable Node.js, the frontend
REM packages and SearXNG must not be replaced under a running SAIVerse.
REM scripts\setup_venv.py check-stopped looks at the runtime markers of the
REM SAIVerse data folder and at this folder's frontend and SearXNG servers,
REM prints what to do, and exits 1 when something runs or it cannot tell.
REM A first install has nothing running and goes on. Same as setup.sh.
"%PYTHON_EXE%" scripts\setup_venv.py check-stopped
if errorlevel 1 goto :saiverse_running

REM --- 2. Node.js check & auto-install ---
REM A portable install from a previous setup comes first, as in start.bat.
REM Bring it to the version install_node_portable.ps1 pins; if that fails the
REM existing .node\ is left as it was and setup continues with it. A .node.old\
REM alone is a replacement that died half-way; the script puts it back first.
set "PORTABLE_NODE="
if exist ".node\node.exe" set "PORTABLE_NODE=1"
if exist ".node.old\node.exe" set "PORTABLE_NODE=1"
if defined PORTABLE_NODE (
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_node_portable.ps1
    if errorlevel 1 echo [WARN] Could not update the portable Node.js; continuing with the existing one.
)
if exist ".node\node.exe" (
    set "PATH=%CD%\.node;%PATH%"
    goto :node_found
)
where node >nul 2>nul
if %errorlevel% equ 0 goto :node_found
echo.
echo [SETUP] Node.js not found. Attempting auto-install...
REM Try winget first
where winget >nul 2>nul
if %errorlevel% neq 0 goto :try_portable_node
winget install OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements
if %errorlevel% neq 0 goto :try_portable_node
REM Add default install path to current session
set "PATH=%PATH%;C:\Program Files\nodejs"
where node >nul 2>nul
if %errorlevel% neq 0 (
    echo [OK] Node.js installed via winget.
    echo   Please close this window and run setup.bat again to refresh PATH.
    pause
    exit /b 0
)
for /f "tokens=*" %%v in ('node --version') do set NODE_VERSION=%%v
echo [OK] Node.js %NODE_VERSION% installed
goto :node_done

:try_portable_node
echo [SETUP] Installing Node.js portable edition...
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_node_portable.ps1
if %errorlevel% neq 0 (
    echo [ERROR] Could not install Node.js automatically.
    echo   Please install manually from https://nodejs.org/
    pause
    exit /b 1
)
if not exist ".node\node.exe" (
    echo [ERROR] Node.js portable installation failed.
    echo   Please install manually from https://nodejs.org/
    pause
    exit /b 1
)
set "PATH=%CD%\.node;%PATH%"
for /f "tokens=*" %%v in ('node --version') do set NODE_VERSION=%%v
echo [OK] Node.js %NODE_VERSION% installed (portable)
goto :node_done

:node_found
for /f "tokens=*" %%v in ('node --version') do set NODE_VERSION=%%v
echo [OK] Node.js %NODE_VERSION%

:node_done

REM Restore working directory (winget/MSI install can change CWD to System32)
cd /d "%~dp0"

REM --- 2b. Visual C++ runtime check & auto-install ---
REM onnxruntime (memory search) needs the Microsoft Visual C++ Redistributable,
REM which a freshly installed Windows does not have. Without it SAIVerse fails
REM at startup. The script prints its own messages and does not stop setup.
powershell -ExecutionPolicy Bypass -File scripts\install_vc_redist.ps1

REM --- 3. Create or rebuild .venv ---
REM scripts\setup_venv.py prepare, run with the python chosen above, prints one
REM line: keep / create / renamed <old name> / resume <old name> / locked. An
REM existing .venv made with another Python series than 3.13, or a broken one,
REM is not deleted: it is renamed to .venv.old-<date>-<time> and deleted only
REM after the new .venv has its packages and the addon packages were installed
REM again. If the new one fails, the old one is put back. resume means an
REM earlier setup stopped after it made the new .venv: it is used as it is, and
REM the rest runs as for renamed. When the rename is refused because files in
REM .venv are in use, nothing changes and setup stops. With
REM SAIVERSE_ALLOW_NON_RECOMMENDED_PYTHON=1 the Python series of an existing
REM .venv is not checked.
set "VENV_CHECK_ARGS=--expect-series 3.13"
if defined ALLOW_OTHER_PYTHON set "VENV_CHECK_ARGS="
if defined ALLOW_OTHER_PYTHON echo [INFO] SAIVERSE_ALLOW_NON_RECOMMENDED_PYTHON=1: the Python series of an existing .venv is not checked.
set "VENV_ACTION="
set "VENV_OLD="
set "ADDON_REBUILD_ARGS="
for /f "tokens=1,*" %%a in ('call "%PYTHON_EXE%" scripts\setup_venv.py prepare %VENV_CHECK_ARGS%') do (
    set "VENV_ACTION=%%a"
    set "VENV_OLD=%%b"
)
if "%VENV_ACTION%"=="keep" goto :venv_keep
if "%VENV_ACTION%"=="create" goto :venv_create
if "%VENV_ACTION%"=="renamed" goto :venv_create
if "%VENV_ACTION%"=="resume" goto :venv_resume
if "%VENV_ACTION%"=="locked" goto :venv_locked
echo [ERROR] Could not check the existing .venv folder. See the messages above.
pause
exit /b 1

:venv_locked
pause
exit /b 1

:venv_create
if not "%VENV_ACTION%"=="renamed" set "VENV_OLD="
set "ADDON_REBUILD_ARGS=--core-venv-recreated"
echo.
echo [SETUP] Creating Python virtual environment...
"%PYTHON_EXE%" -m venv .venv
if errorlevel 1 goto :venv_create_failed
if not exist ".venv\Scripts\activate.bat" goto :venv_incomplete
echo [OK] Created .venv
goto :venv_ready

:venv_create_failed
echo [ERROR] Failed to create virtual environment.
call :restore_old_venv
pause
exit /b 1

:venv_incomplete
echo [ERROR] Virtual environment was created but is incomplete.
echo   .venv\Scripts\activate.bat was not generated.
echo.
echo   This usually means your Python installation is missing "ensurepip".
echo   Common causes:
echo     - Microsoft Store version of Python: uninstall it and use python.org
echo     - Python installed without pip: reinstall it with default options
echo.
echo   To fix:
echo     1. Uninstall your current Python
echo     2. Download Python 3.13.17 from https://www.python.org/downloads/release/python-31317/
echo     3. Run installer, check "Add python.exe to PATH", use default settings
echo     4. Run setup.bat again
if defined VENV_OLD goto :venv_incomplete_restore
rmdir /s /q .venv
pause
exit /b 1

:venv_incomplete_restore
call :restore_old_venv
pause
exit /b 1

:venv_keep
echo [OK] .venv already exists
goto :venv_ready

:venv_resume
set "ADDON_REBUILD_ARGS=--core-venv-recreated"
echo [OK] Continuing with the .venv made by the previous setup

:venv_ready

REM --- 4. Activate venv ---
call .venv\Scripts\activate.bat

REM --- 5. pip install ---
echo.
echo [SETUP] Installing Python packages...
python -m pip install --upgrade pip >nul 2>nul
python -m pip install -r requirements.lock
if errorlevel 1 goto :pip_failed
echo [OK] Python packages installed

REM --- 5b. Reinstall addon packages, then delete the old .venv ---
REM When this run created a new .venv (create / renamed / resume),
REM --core-venv-recreated makes the script first run again, with the saved
REM answers, the addon setup steps that install packages into .venv
REM (pip_install without env). Then addon environments made with another
REM Python series are rebuilt with the saved answers. Their failures do not
REM stop setup: SAIVerse itself works, and the script prints what to do and
REM records the failures in .addon_env_rebuild_failures.json, which the next
REM setup reads. Only then .venv.old-<date>-<time> folders are deleted: a setup
REM stopped before this point leaves the old one, and the next setup resumes.
REM Same as setup.sh.
python scripts\rebuild_addon_envs.py %ADDON_REBUILD_ARGS%
if errorlevel 1 echo [WARN] Could not check the addon packages. SAIVerse itself works; see the messages above.
"%PYTHON_EXE%" scripts\setup_venv.py finish

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

REM --- 7. Database seed (only if not exists or core tables missing) ---
set SAIVERSE_DB=%USERPROFILE%\.saiverse\user_data\database\saiverse.db
set SAIVERSE_DB_LEGACY=database\data\saiverse.db
set NEED_SEED=1

if exist "%SAIVERSE_DB%" (
    python -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('SELECT 1 FROM city LIMIT 1'); c.close()" "%SAIVERSE_DB%" >nul 2>nul
    if !errorlevel! equ 0 set NEED_SEED=0
)
if "!NEED_SEED!"=="1" if exist "%SAIVERSE_DB_LEGACY%" (
    python -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('SELECT 1 FROM city LIMIT 1'); c.close()" "%SAIVERSE_DB_LEGACY%" >nul 2>nul
    if !errorlevel! equ 0 set NEED_SEED=0
)

if "!NEED_SEED!"=="1" (
    if exist "%SAIVERSE_DB%" (
        echo.
        echo [WARN] Database file exists but core tables are missing. Re-initializing...
    )
    echo.
    echo [SETUP] Initializing database...
    python database\seed.py --force
    if !errorlevel! neq 0 (
        echo [ERROR] Database initialization failed.
        pause
        exit /b 1
    )
    echo [OK] Database initialized
) else (
    echo [OK] Database already exists
)

REM --- 7b. Import/update playbooks ---
echo.
echo [SETUP] Updating playbooks...
python scripts\import_all_playbooks.py --force
if !errorlevel! neq 0 (
    echo [WARN] Playbook update failed. You can retry later with: python scripts\import_all_playbooks.py --force
) else (
    echo [OK] Playbooks updated
)

REM --- 8. Create expansion_data directory ---
if not exist "expansion_data" (
    mkdir expansion_data
    echo [OK] Created expansion_data directory
) else (
    echo [OK] expansion_data already exists
)

REM --- 9. Git check & auto-install ---
where git >nul 2>nul
if %errorlevel% equ 0 goto :git_ready
REM Check for portable install from previous setup
if exist ".git-portable\cmd\git.exe" (
    set "PATH=%CD%\.git-portable\cmd;%PATH%"
    goto :git_ready
)
echo.
echo [SETUP] Git not found. Attempting auto-install...
where winget >nul 2>nul
if %errorlevel% neq 0 goto :try_portable_git
winget install --id Git.Git -e --accept-package-agreements --accept-source-agreements
REM Add default install path to current session
set "PATH=%PATH%;C:\Program Files\Git\cmd"
where git >nul 2>nul
if %errorlevel% equ 0 goto :git_ready
goto :try_portable_git

:try_portable_git
echo [SETUP] Installing Git portable edition...
powershell -ExecutionPolicy Bypass -File scripts\install_git_portable.ps1
if not exist ".git-portable\cmd\git.exe" (
    echo [WARN] Could not install Git automatically.
    echo   Auto-update via update.bat will not be available.
    echo   You can install Git manually from https://git-scm.com/ and run setup.bat again.
    goto :git_skip
)
set "PATH=%CD%\.git-portable\cmd;%PATH%"

:git_ready
REM Restore working directory (winget may change CWD to System32)
cd /d "%~dp0"
for /f "tokens=*" %%v in ('git --version') do set GIT_VERSION=%%v
echo [OK] %GIT_VERSION%

REM --- 9b. Initialize git repository ---
REM scripts\init_git_repo.py records the release this folder actually contains
REM (the tag named after VERSION), not always the newest one, and tells the user
REM when the files do not match the record. It prints its own messages, and its
REM warnings do not stop setup. setup.sh calls the same script.
python scripts\init_git_repo.py

:git_skip

REM --- 10. Create .env from example if not exists ---
if not exist ".env" (
    echo.
    echo [SETUP] Creating .env file...
    copy .env.example .env >nul
    echo [OK] Created .env
    echo   API keys can be configured in the first-run tutorial.
) else (
    echo [OK] .env already exists
)

REM --- 11. SearXNG setup ---
echo.
echo [SETUP] Setting up SearXNG (web search engine)...
powershell -ExecutionPolicy Bypass -File scripts\setup_searxng.ps1
if %errorlevel% neq 0 (
    echo [WARN] SearXNG setup failed. Web search will be unavailable, but the app still works.
) else (
    echo [OK] SearXNG setup complete
)

REM --- 12. Pre-download embedding model ---
echo.
echo [SETUP] Downloading embedding model (first time only, may take a few minutes)...
REM Same download as the first launch (pinned files of sai_memory/config.py), then a test load.
python -c "from sai_memory.memory.recall import Embedder; Embedder()"
if %errorlevel% neq 0 (
    echo [WARN] Embedding model download failed. It will retry on first launch.
) else (
    echo [OK] Embedding model downloaded
)

REM --- 13. Complete ---
echo.
echo ========================================
echo   Setup Complete!
echo ========================================
if exist "VERSION" (
    set /p CURRENT_VER=<VERSION
    echo   Version: !CURRENT_VER!
)
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
exit /b 0

REM ---------------------------------------------------------------
REM Subroutines
REM ---------------------------------------------------------------

:saiverse_running
pause
exit /b 1

:pip_failed
echo [ERROR] pip install failed.
call :restore_old_venv
pause
exit /b 1

REM Put the .venv renamed by this run back, after the new one failed.
:restore_old_venv
if not defined VENV_OLD exit /b 0
"%PYTHON_EXE%" scripts\setup_venv.py rollback "%VENV_OLD%"
exit /b 0

REM Set PYTHON_EXE to the python.exe of the Python 3.13 registered under
REM %1\Software\Python\PythonCore\3.13\InstallPath, when it exists and runs
REM as 3.13. The value line looks like "(Default)    REG_SZ    C:\Python313\";
REM the name of the default value differs by Windows language, so the folder
REM is taken from what follows REG_SZ.
:find_python313
set "PY313_LINE="
set "PY313_DIR="
for /f "delims=" %%L in ('reg query "%~1\Software\Python\PythonCore\3.13\InstallPath" /ve 2^>nul ^| findstr /c:"REG_SZ"') do set "PY313_LINE=%%L"
if not defined PY313_LINE exit /b 0
set "PY313_DIR=!PY313_LINE:*REG_SZ=!"
for /f "tokens=*" %%D in ("!PY313_DIR!") do set "PY313_DIR=%%D"
if not defined PY313_DIR exit /b 0
if "!PY313_DIR:~-1!"=="\" set "PY313_DIR=!PY313_DIR:~0,-1!"
if not exist "!PY313_DIR!\python.exe" exit /b 0
"!PY313_DIR!\python.exe" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)" >nul 2>nul
if errorlevel 1 exit /b 0
set "PYTHON_EXE=!PY313_DIR!\python.exe"
exit /b 0

REM Print scripts\setup_messages\%1.txt. The file is UTF-8 with Japanese and
REM English; PowerShell reads it as UTF-8, which "type" would not.
:show_message
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -LiteralPath 'scripts\setup_messages\%~1.txt' -Encoding UTF8"
if errorlevel 1 type "scripts\setup_messages\%~1.txt"
exit /b 0
