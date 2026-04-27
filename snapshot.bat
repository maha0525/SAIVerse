@echo off
cd /d "%~dp0"
setlocal

REM Thin wrapper around scripts/snapshot.py.
REM Usage: snapshot.bat {save|list|restore|inspect|delete} [args...]

if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] .venv not found. Please run setup.bat first.
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"

REM No arguments: print help and pause so the window doesn't disappear when
REM the user double-clicks the bat file from Explorer.
if "%~1"=="" (
    python "scripts\snapshot.py" --help
    echo.
    echo ----------------------------------------------------------------
    echo This tool requires a subcommand. Run from a terminal, e.g.:
    echo   snapshot.bat list
    echo   snapshot.bat save my_snapshot --note "before upgrade"
    echo   snapshot.bat inspect my_snapshot
    echo   snapshot.bat restore my_snapshot
    echo   snapshot.bat delete my_snapshot
    echo ----------------------------------------------------------------
    echo.
    pause
    exit /b 0
)

python "scripts\snapshot.py" %*
exit /b %errorlevel%
