
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

$rawSrc = if ($env:SEARXNG_SRC_DIR) { $env:SEARXNG_SRC_DIR } else { "scripts\.searxng-src" }
$SRC_DIR = Get-AbsPath $rawSrc

$rawVenv = if ($env:SEARXNG_VENV_DIR) { $env:SEARXNG_VENV_DIR } else { "scripts\.searxng-venv" }
$VENV_DIR = Get-AbsPath $rawVenv

$rawSettings = if ($env:SEARXNG_SETTINGS_PATH) { $env:SEARXNG_SETTINGS_PATH } else { "scripts\searxng_settings.yml" }
$SETTINGS_PATH = Get-AbsPath $rawSettings

$venvPython = "$VENV_DIR\Scripts\python.exe"

# Run setup if not done yet
if (-not (Test-Path "$SRC_DIR\requirements.txt") -or -not (Test-Path $venvPython)) {
    Write-Host "[INFO] SearXNG is not set up. Running setup..."
    $setupScript = Join-Path $ScriptRoot "setup_searxng.ps1"
    & $setupScript
}

# Start server
$env:SEARXNG_SETTINGS_PATH = "$SETTINGS_PATH"
$env:SEARXNG_PORT = "$PORT"
$env:SEARXNG_BIND_ADDRESS = "$BIND_ADDRESS"
$env:FLASK_SKIP_DOTENV = "1"

Write-Host "[INFO] Starting SearXNG at http://${BIND_ADDRESS}:${PORT}"
& $venvPython -m searx.webapp
