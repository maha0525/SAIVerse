@echo off
rem Frontend dev server pointed at the ISOLATED test backend (ASCII only - see CLAUDE.md).
rem Pair of start_test_server.bat: run that first (backend on 18000), then this (UI on 18010).
rem Without SAIVERSE_BACKEND_ORIGIN the frontend proxies /api to 127.0.0.1:8000,
rem which is the PRODUCTION backend - never use this script without the env var below.
setlocal
set "SAIVERSE_BACKEND_ORIGIN=http://127.0.0.1:18000"
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "PROJECT_ROOT=%%~fI"
cd /d "%PROJECT_ROOT%"
call npm --prefix frontend run dev -- --port 18010
