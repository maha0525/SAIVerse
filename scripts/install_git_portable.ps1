# Download and install Git for Windows portable to .git-portable/ directory
# Called from setup.bat when neither system Git nor winget-installed Git is available.

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$GitDir = Join-Path $ProjectRoot ".git-portable"
$GitExe = Join-Path $GitDir "cmd\git.exe"

# Skip if already installed
if (Test-Path $GitExe) {
    $ver = & $GitExe --version 2>$null
    Write-Host "[OK] Git portable already installed ($ver)"
    exit 0
}

# Fetch latest release info from GitHub
$ApiUrl = "https://api.github.com/repos/git-for-windows/git/releases/latest"
Write-Host "[SETUP] Looking up latest PortableGit release..."
try {
    $release = Invoke-RestMethod -Uri $ApiUrl -Headers @{ "User-Agent" = "SAIVerse-Setup" } -UseBasicParsing
} catch {
    Write-Host "[ERROR] Failed to query GitHub API: $_"
    exit 1
}

$asset = $release.assets | Where-Object { $_.name -like "PortableGit-*-64-bit.7z.exe" } | Select-Object -First 1
if (-not $asset) {
    Write-Host "[ERROR] PortableGit 64-bit asset not found in latest release."
    exit 1
}

$ExeFile = Join-Path $env:TEMP $asset.name
Write-Host "[SETUP] Downloading $($asset.name) (~50MB)..."
try {
    $ProgressPreference = "SilentlyContinue"
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $ExeFile -UseBasicParsing
} catch {
    Write-Host "[ERROR] Download failed: $_"
    exit 1
}

Write-Host "[SETUP] Extracting to .git-portable/ ..."
if (Test-Path $GitDir) { Remove-Item $GitDir -Recurse -Force }
New-Item -ItemType Directory -Path $GitDir | Out-Null

try {
    # PortableGit self-extracting 7z: -o"<path>" specifies output, -y for silent
    $proc = Start-Process -FilePath $ExeFile -ArgumentList "-o`"$GitDir`"", "-y" -Wait -PassThru -NoNewWindow
    if ($proc.ExitCode -ne 0) {
        Write-Host "[ERROR] Extraction failed with exit code $($proc.ExitCode)"
        exit 1
    }
} catch {
    Write-Host "[ERROR] Extraction failed: $_"
    exit 1
} finally {
    Remove-Item $ExeFile -Force -ErrorAction SilentlyContinue
}

# Verify
if (Test-Path $GitExe) {
    $ver = & $GitExe --version 2>$null
    Write-Host "[OK] $ver installed to .git-portable/"
    exit 0
} else {
    Write-Host "[ERROR] git.exe not found after extraction."
    exit 1
}
