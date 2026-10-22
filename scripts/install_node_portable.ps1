# Download and install Node.js portable (ZIP) to .node/ directory
# Called from setup.bat when neither system Node.js nor winget is available.

$ErrorActionPreference = "Stop"

$NodeVersion = "24.22.1"
$Arch = "x64"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$NodeDir = Join-Path $ProjectRoot ".node"
$ZipUrl = "https://nodejs.org/dist/v${NodeVersion}/node-v${NodeVersion}-win-${Arch}.zip"
$ZipFile = Join-Path $env:TEMP "node-v${NodeVersion}-win-${Arch}.zip"

# Skip if already installed
if (Test-Path (Join-Path $NodeDir "node.exe")) {
    $ver = & (Join-Path $NodeDir "node.exe") --version 2>$null
    Write-Host "[OK] Node.js portable already installed ($ver)"
    exit 0
}

Write-Host "[SETUP] Downloading Node.js v${NodeVersion} portable..."
try {
    Invoke-WebRequest -Uri $ZipUrl -OutFile $ZipFile -UseBasicParsing
} catch {
    Write-Host "[ERROR] Download failed: $_"
    exit 1
}

Write-Host "[SETUP] Extracting to .node/ ..."
$TempExtract = Join-Path $env:TEMP "node_extract_$PID"
if (Test-Path $TempExtract) { Remove-Item $TempExtract -Recurse -Force }

try {
    Expand-Archive -Path $ZipFile -DestinationPath $TempExtract
} catch {
    Write-Host "[ERROR] Extraction failed: $_"
    exit 1
}

# Move inner directory (node-v24.22.1-win-x64/) contents to .node/
$InnerDir = Get-ChildItem $TempExtract | Select-Object -First 1
if (Test-Path $NodeDir) { Remove-Item $NodeDir -Recurse -Force }
Move-Item $InnerDir.FullName $NodeDir

# Cleanup
Remove-Item $ZipFile -Force -ErrorAction SilentlyContinue
Remove-Item $TempExtract -Recurse -Force -ErrorAction SilentlyContinue

# Verify
if (Test-Path (Join-Path $NodeDir "node.exe")) {
    $ver = & (Join-Path $NodeDir "node.exe") --version 2>$null
    Write-Host "[OK] Node.js $ver installed to .node/"
    exit 0
} else {
    Write-Host "[ERROR] node.exe not found after extraction."
    exit 1
}
