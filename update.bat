@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run setup.bat first.
    exit /b 1
)
".venv\Scripts\python.exe" "scripts\update_engine.py" --manual
exit /b %errorlevel%
