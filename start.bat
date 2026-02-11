@echo off
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

REM Start Backend
echo [INFO] Starting backend...
start "SAIVerse Backend" /min cmd /c "call .venv\Scripts\activate.bat && python main.py city_a"

REM Wait for backend to initialize
echo [INFO] Waiting for backend to initialize...
timeout /t 5 /nobreak >nul

REM Start Frontend
echo [INFO] Starting frontend...
start "SAIVerse Frontend" /min cmd /c "cd frontend && npm run dev"

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
echo   To stop: close all the command prompt windows.
echo.
echo   You can close this window.
echo.
pause
