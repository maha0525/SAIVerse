$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw ".venv not found. Run setup.bat first."
}
& $Python (Join-Path $PSScriptRoot "update_engine.py") --manual
exit $LASTEXITCODE
