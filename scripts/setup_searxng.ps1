
$ErrorActionPreference = "Stop"

# SearXNG setup script (idempotent).
# Clones the SearXNG source (with sparse-checkout for Windows compatibility),
# creates a dedicated venv, installs dependencies, patches Windows-incompatible
# code, and prepares settings / limiter configuration.
#
# This script is called from setup.bat during initial setup
# and also from run_searxng_server.ps1 as a fallback.

$ScriptRoot = $PSScriptRoot
if (-not $ScriptRoot) {
    if (Test-Path ".\scripts\setup_searxng.ps1") {
        $ScriptRoot = (Resolve-Path ".\scripts").Path
    } elseif (Test-Path ".\setup_searxng.ps1") {
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
$rawSrc = if ($env:SEARXNG_SRC_DIR) { $env:SEARXNG_SRC_DIR } else { "scripts\.searxng-src" }
$SRC_DIR = Get-AbsPath $rawSrc

$rawVenv = if ($env:SEARXNG_VENV_DIR) { $env:SEARXNG_VENV_DIR } else { "scripts\.searxng-venv" }
$VENV_DIR = Get-AbsPath $rawVenv

$rawSettings = if ($env:SEARXNG_SETTINGS_PATH) { $env:SEARXNG_SETTINGS_PATH } else { "scripts\searxng_settings.yml" }
$SETTINGS_PATH = Get-AbsPath $rawSettings

$BRANCH_OR_TAG = if ($env:SEARXNG_REF) { $env:SEARXNG_REF } else { "master" }

Write-Host "[INFO] Setting up SearXNG..."
Write-Host "[INFO] Workspace Root: $WorkspaceRoot"
Write-Host "[INFO] Source Dir: $SRC_DIR"

# --- Setup Source ---
$shouldClone = $true
if (Test-Path $SRC_DIR) {
    if (Test-Path "$SRC_DIR\requirements.txt") {
        $shouldClone = $false
    } else {
        Write-Host "[WARN] $SRC_DIR exists but seems corrupt (missing requirements.txt). Removing..."
        Remove-Item -Recurse -Force $SRC_DIR
    }
}

if ($shouldClone) {
    Write-Host "[INFO] Cloning SearXNG source into $SRC_DIR (ref=$BRANCH_OR_TAG) with sparse-checkout"

    git clone --filter=blob:none --no-checkout --depth 1 --branch "$BRANCH_OR_TAG" https://github.com/searxng/searxng.git "$SRC_DIR"
    if ($LASTEXITCODE -ne 0) { throw "Git clone failed" }

    Push-Location "$SRC_DIR"
    try {
        git sparse-checkout init --cone
        # Allow Git to process index entries with invalid Windows characters (like colons)
        git config core.protectNTFS false
        git sparse-checkout set searx requirements.txt
        git checkout "$BRANCH_OR_TAG"
        if ($LASTEXITCODE -ne 0) { throw "Git checkout failed" }
    } finally {
        Pop-Location
    }
}

# --- Setup Venv ---
$venvPython = "$VENV_DIR\Scripts\python.exe"
if (Test-Path $venvPython) {
    $checkCmd = "import importlib.util; import sys; sys.exit(0 if importlib.util.find_spec('searx') else 1)"
    & $venvPython -c $checkCmd 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[INFO] SearXNG venv already set up"
    } else {
        Write-Host "[INFO] Existing virtualenv found but SearXNG is not installed. Reinstalling..."
    }
} else {
    Write-Host "[INFO] Creating virtualenv at $VENV_DIR"
    python -m venv "$VENV_DIR"
    if ($LASTEXITCODE -ne 0) { throw "Failed to create venv" }
}

# Only install if searx is not yet importable
$needsInstall = $true
if (Test-Path $venvPython) {
    & $venvPython -c "import importlib.util; import sys; sys.exit(0 if importlib.util.find_spec('searx') else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { $needsInstall = $false }
}

if ($needsInstall) {
    Write-Host "[INFO] Upgrading pip..."
    & $venvPython -m pip install --upgrade pip setuptools wheel

    Write-Host "[INFO] Installing SearXNG runtime dependencies..."
    & $venvPython -m pip install -r "$SRC_DIR\requirements.txt"
    & $venvPython -m pip install --no-build-isolation -e "$SRC_DIR"
}

# --- Prepare Settings ---
if (-not (Test-Path $SETTINGS_PATH)) {
    Write-Host "[INFO] Preparing settings from $SRC_DIR\searx\settings.yml"
    & $venvPython -m pip show PyYAML | Out-Null
    if ($LASTEXITCODE -ne 0) { & $venvPython -m pip install pyyaml }
    Copy-Item "$SRC_DIR\searx\settings.yml" -Destination "$SETTINGS_PATH"
}

$prepareSettingsScript = @"
import sys
import yaml
from pathlib import Path
import os
import secrets
import re

path = Path(sys.argv[1])
if not path.exists():
    sys.exit(1)

data = yaml.safe_load(path.read_text(encoding='utf-8'))

# allow JSON output by default
search = data.setdefault("search", {})
formats = search.get("formats") or []
if "json" not in formats:
    formats.append("json")
search["formats"] = formats
search.setdefault("safe_search", 1)

# bind on all interfaces for local network use
server = data.setdefault("server", {})
if server.get("bind_address") == "127.0.0.1":
    server["bind_address"] = "0.0.0.0"

default_secret = "ultrasecretkey"
env_secret = os.getenv("SEARXNG_SECRET_KEY")
if env_secret:
    server["secret_key"] = env_secret
elif not server.get("secret_key") or server.get("secret_key") == default_secret:
    server["secret_key"] = secrets.token_hex(32)

# disable engines that frequently fail without extra deps or network access.
problematic_engines = {"ahmia", "torch", "wikidata", "radiobrowser"}

def normalize(name: str) -> str:
    return re.sub(r"[\s_-]+", "", name.lower())

engines = []
for engine in data.get("engines", []):
    name = engine.get("name") or ""
    if normalize(name) in problematic_engines:
        continue
    engines.append(engine)

data["engines"] = engines

path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding='utf-8')
"@

$settingsScriptPath = Join-Path $ScriptRoot "prepare_settings_temp.py"
Set-Content -Path $settingsScriptPath -Value $prepareSettingsScript -Encoding UTF8
& $venvPython $settingsScriptPath "$SETTINGS_PATH"
Remove-Item $settingsScriptPath

# --- Patch for Windows Compatibility (pwd module) ---
Write-Host "[INFO] Patching sources for Windows compatibility..."
$patchScript = @"
import sys
from pathlib import Path

# searx/valkeydb.py uses 'pwd' which is not available on Windows
target = Path(sys.argv[1]) / 'searx' / 'valkeydb.py'
if target.exists():
    content = target.read_text(encoding='utf-8')
    if 'import pwd' in content and 'class MockPwd:' not in content:
        print(f'Patching {target}...')
        new_content = content.replace('import pwd', '''try:
    import pwd
except ImportError:
    class MockPwd:
        def getpwuid(self, uid):
            return ['searx']
    pwd = MockPwd()''')
        target.write_text(new_content, encoding='utf-8')
"@
$patchScriptPath = Join-Path $ScriptRoot "patch_windows.py"
Set-Content -Path $patchScriptPath -Value $patchScript -Encoding UTF8
& $venvPython $patchScriptPath "$SRC_DIR"
Remove-Item $patchScriptPath

# --- Prepare Limiter ---
$rawLimiter = if ($env:SEARXNG_LIMITER_PATH) { $env:SEARXNG_LIMITER_PATH } else { "scripts\limiter.toml" }
$limiterPath = Get-AbsPath $rawLimiter

Write-Host "[INFO] Regenerating limiter configuration at $limiterPath"

$prepareLimiterScript = @"
import sys
from pathlib import Path
import shutil
import importlib.resources as resources

dest = Path(sys.argv[1])
src_dir = Path(sys.argv[2])
dest.parent.mkdir(parents=True, exist_ok=True)

def copy_candidate(path: Path) -> bool:
    if path.is_file():
        shutil.copyfile(path, dest)
        return True
    return False

try:
    package_template = resources.files("searx").joinpath("limiter.toml")
except Exception:
    package_template = None

if package_template and copy_candidate(Path(package_template)):
    sys.exit(0)

if copy_candidate(src_dir / "searx" / "limiter.toml"):
    sys.exit(0)

# Last resort
dest.write_text("""[botdetection]\n\nipv4_prefix = 32\nipv6_prefix = 48\n\n[botdetection.ip_limit]\nfilter_link_local = false\nlink_token = false\n\n[botdetection.ip_lists]\nblock_ip = [\n]\npass_ip = [\n  '127.0.0.0/8',\n]\npass_searxng_org = true\n\n[botdetection]\nlink_token = false\nip_limit = false\n""", encoding="utf-8")
"@

$limiterScriptPath = Join-Path $ScriptRoot "prepare_limiter_temp.py"
Set-Content -Path $limiterScriptPath -Value $prepareLimiterScript -Encoding UTF8
& $venvPython $limiterScriptPath "$limiterPath" "$SRC_DIR"
Remove-Item $limiterScriptPath

Write-Host "[OK] SearXNG setup complete"
