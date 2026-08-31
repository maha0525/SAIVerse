@echo off
REM ==========================================================================
REM  Regenerate the auto-generated reference docs:
REM    docs\reference\tool-catalog.md      (<- tools.TOOL_SCHEMAS)
REM    docs\reference\api-endpoints.md     (<- api.main.api_router)
REM    docs\reference\database-schema.md   (<- database.models.Base.metadata)
REM
REM  Run this BEFORE committing changes to tools (builtin_data\tools\),
REM  API routes (api\routes\), or DB models (database\models.py), and include
REM  the regenerated docs in the same commit.
REM  See CLAUDE.md > Important Conventions > Documentation Maintenance.
REM
REM  (Non-Windows devs: run `python scripts/gen_reference_docs.py` directly.)
REM ==========================================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] .venv not found. Please run setup.bat first.
    exit /b 1
)

call .venv\Scripts\activate.bat

echo [gen-docs] Regenerating reference docs from code...
python scripts\gen_reference_docs.py
if errorlevel 1 (
    echo [ERROR] Reference doc generation failed. See output above.
    exit /b 1
)

echo [OK] Reference docs regenerated. Review and commit the changes under docs\reference\.
endlocal
