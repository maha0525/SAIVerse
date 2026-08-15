@echo off
rem Windows twin of start_test_server.sh (ASCII only - see CLAUDE.md notes).
rem Starts the SAIVerse test server against the isolated test_data/ environment.
setlocal
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "PROJECT_ROOT=%%~fI"
set "SAIVERSE_HOME=%PROJECT_ROOT%\test_data\.saiverse"
set "SAIVERSE_USER_DATA_DIR=%PROJECT_ROOT%\test_data\user_data"
set "TEST_DB_PATH=%SAIVERSE_USER_DATA_DIR%\database\saiverse.db"

if not exist "%TEST_DB_PATH%" (
    echo Test environment not found. Running setup...
    "%PROJECT_ROOT%\.venv\Scripts\python.exe" "%SCRIPT_DIR%setup_test_env.py"
)

cd /d "%PROJECT_ROOT%"
"%PROJECT_ROOT%\.venv\Scripts\python.exe" main.py test_city --db-file "%TEST_DB_PATH%"
