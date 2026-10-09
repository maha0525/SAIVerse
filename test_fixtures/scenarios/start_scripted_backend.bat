@echo off
rem Test backend (port 18000) for the scripted fake-LLM scenarios. ASCII only.
rem Same isolation as test_fixtures\start_test_server.bat, plus:
rem  - SAIVERSE_LOG_PATH points into test_data (docs/issues/import_time_log_handlers_escape_isolation.md)
rem  - every cloud LLM / external API key is overwritten with a dead value,
rem    and the world default models point at the scripted fake LLM, so no real
rem    LLM can be called from this process.
rem Values must stay NON-EMPTY: in cmd, set "VAR=" deletes the variable and
rem load_dotenv() then refills it from the repository .env.
rem Start test_fixtures\scenarios\scripted_llm_server.py serve first (port 18097).
setlocal
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..\..") do set "PROJECT_ROOT=%%~fI"
set "SAIVERSE_HOME=%PROJECT_ROOT%\test_data\.saiverse"
set "SAIVERSE_USER_DATA_DIR=%PROJECT_ROOT%\test_data\user_data"
set "TEST_DB_PATH=%SAIVERSE_USER_DATA_DIR%\database\saiverse.db"
set "SCRIPTED_DIR=%PROJECT_ROOT%\test_data\scripted_llm"
if not exist "%SCRIPTED_DIR%" mkdir "%SCRIPTED_DIR%"

rem --- Discord gateway off (same values as start_test_server.bat) ---
set "SAIVERSE_GATEWAY_ENABLED=0"
set "SAIVERSE_GATEWAY_WS_URL=ws://127.0.0.1:9/test-disabled"
set "SAIVERSE_GATEWAY_TOKEN=test-disabled"
set "SAIVERSE_GATEWAY_CHANNEL_MAP=[]"

rem --- logs and dumps stay inside test_data ---
set "SAIVERSE_LOG_PATH=%SAIVERSE_HOME%\log.txt"
set "SAIVERSE_LLM_CONTEXT_DUMP=%SCRIPTED_DIR%\llm_context.log"
set "SAIVERSE_SEA_DUMP=%SCRIPTED_DIR%\sea_dump.log"
set "SAIVERSE_DB_BACKUP_ON_START=false"
set "SAIMEMORY_BACKUP_ON_START=false"
set "SDS_URL=http://127.0.0.1:9"

rem --- no real LLM / external API from this process ---
set "GEMINI_API_KEY=test-disabled"
set "GEMINI_FREE_API_KEY=test-disabled"
set "OPENAI_API_KEY=test-disabled"
set "CLAUDE_API_KEY=test-disabled"
set "ANTHROPIC_API_KEY=test-disabled"
set "OPENROUTER_API_KEY=test-disabled"
set "XAI_API_KEY=test-disabled"
set "NVIDIA_API_KEY=test-disabled"
set "MOONSHOT_API_KEY=test-disabled"
set "PLAMO_API_KEY=test-disabled"
set "SAKANA_API_KEY=test-disabled"
set "TYPESAFE_API_KEY=test-disabled"
set "KOBOLDCPP_API_KEY=test-disabled"
set "LMSTUDIO_API_KEY=test-disabled"
set "LLAMA_SERVER_API_KEY=test-disabled"
set "X_API_KEY=test-disabled"
set "X_API_SECRET=test-disabled"
set "SMTP_HOST=127.0.0.1"
set "SMTP_PORT=9"
set "SMTP_USERNAME=test-disabled"
set "SMTP_PASSWORD=test-disabled"

rem --- world default models -> scripted fake LLM ---
set "SAIVERSE_DEFAULT_MODEL=scripted-llm"
set "SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL=scripted-llm"
set "SAIVERSE_REFLEX_JUDGMENT_MODEL=scripted-llm"
set "SAIVERSE_AGENTIC_MODEL=scripted-llm"
set "SAIVERSE_PULSE_MODEL=scripted-llm"
set "SAIVERSE_TASK_CREATION_MODEL=scripted-llm"
set "SAIVERSE_IMAGE_SUMMARY_MODEL=scripted-llm"
set "MEMORY_WEAVE_MODEL=scripted-llm"

if not exist "%TEST_DB_PATH%" (
    echo Test environment not found. Running setup...
    "%PROJECT_ROOT%\.venv\Scripts\python.exe" "%PROJECT_ROOT%\test_fixtures\setup_test_env.py"
)

cd /d "%PROJECT_ROOT%"
"%PROJECT_ROOT%\.venv\Scripts\python.exe" main.py test_city --db-file "%TEST_DB_PATH%"
