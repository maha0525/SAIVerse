
$ErrorActionPreference = "Stop"

# Launch a local SearXNG server.
# Setup is handled by setup_searxng.ps1 (called from setup.bat).
# If setup hasn't been done yet, this script runs it automatically.

$ScriptRoot = $PSScriptRoot
if (-not $ScriptRoot) {
    if (Test-Path ".\scripts\run_searxng_server.ps1") {
        $ScriptRoot = (Resolve-Path ".\scripts").Path
    } elseif (Test-Path ".\run_searxng_server.ps1") {
        $ScriptRoot = (Get-Location).Path
    } else {
        $ScriptRoot = (Get-Location).Path
    }
}

$WorkspaceRoot = (Join-Path $ScriptRoot "..")

function Get-AbsPath {
    param($Path)
    if ([System.IO.Path]::IsPathRooted($Path)) { return $Path }
    return Join-Path $WorkspaceRoot $Path
}

# Configuration
$PORT = if ($env:SEARXNG_PORT) { $env:SEARXNG_PORT } else { "8080" }
$BIND_ADDRESS = if ($env:SEARXNG_BIND_ADDRESS) { $env:SEARXNG_BIND_ADDRESS } else { "0.0.0.0" }

$rawVenv = if ($env:SEARXNG_VENV_DIR) { $env:SEARXNG_VENV_DIR } else { "scripts\.searxng-venv" }
$VENV_DIR = Get-AbsPath $rawVenv

$rawSettings = if ($env:SEARXNG_SETTINGS_PATH) { $env:SEARXNG_SETTINGS_PATH } else { "scripts\searxng_settings.yml" }
$SETTINGS_PATH = Get-AbsPath $rawSettings

$venvPython = "$VENV_DIR\Scripts\python.exe"
# The installer needs only this checkout and the standard library (certifi,
# when present, serves HTTPS trust via saiverse.tls_trust) and runs with
# SAIVerse's own .venv python, as setup.bat and the update run it: it then checks the SearXNG venv
# against the Python series SAIVerse runs, and makes it again when SAIVerse
# moved to another series. A SearXNG setup that stopped part-way
# (.setup_in_progress\searxng-venv.json, scripts/setup_in_progress.py) may
# have left no SearXNG venv python, or only a half-made one, and is finished
# the same way. Not whatever "python" is first on PATH (start.bat does not
# activate .venv). See docs/intent/dependency_management.md section 2-7.
$saiversePython = Join-Path $WorkspaceRoot ".venv\Scripts\python.exe"
if (Test-Path $saiversePython) {
    $bootstrapPython = $saiversePython
} elseif (Test-Path $venvPython) {
    $bootstrapPython = $venvPython
} else {
    $bootstrapPython = "python"
}
$installer = Join-Path $ScriptRoot "searxng_install.py"
$SRC_DIR = & $bootstrapPython $installer source
if ($LASTEXITCODE -ne 0) { throw "Invalid SearXNG source configuration" }
$env:SEARXNG_SRC_DIR = "$SRC_DIR"

# Check the installed commit and real imports, including timezone data.
& $bootstrapPython $installer check
if ($LASTEXITCODE -ne 0) {
    Write-Host "[INFO] Preparing the pinned SearXNG release..."
    & $bootstrapPython $installer setup
    if ($LASTEXITCODE -ne 0) { throw "SearXNG setup failed" }
}

# Re-merge settings (picks up SAIVerse default changes + user overrides)
Write-Host "[INFO] Merging SearXNG settings..."
$mergeScript = Join-Path $ScriptRoot "merge_searxng_settings.py"
& $venvPython $mergeScript
if ($LASTEXITCODE -ne 0) { throw "SearXNG settings generation failed" }

# SearXNG asks git for its version at start-up and dies when git cannot be
# found (Git installed only inside the SAIVerse folder). Let it start without git.
$versionScript = Join-Path $ScriptRoot "ensure_searxng_version.py"
& $venvPython $versionScript "$SRC_DIR"
if ($LASTEXITCODE -ne 0) { throw "SearXNG version preparation failed" }

# Start server
$env:SEARXNG_SETTINGS_PATH = "$SETTINGS_PATH"
$env:SEARXNG_PORT = "$PORT"
$env:SEARXNG_BIND_ADDRESS = "$BIND_ADDRESS"
$env:FLASK_SKIP_DOTENV = "1"

Write-Host "[INFO] Starting SearXNG at http://${BIND_ADDRESS}:${PORT}"
& $venvPython -m searx.webapp
exit $LASTEXITCODE
