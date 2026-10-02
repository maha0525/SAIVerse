# Install the Microsoft Visual C++ Redistributable (x64) when it is missing.
# Called from setup.bat.
#
# onnxruntime (used by memory search) loads msvcp140.dll and friends. They are
# not part of Windows: most PCs have them because some other application
# brought them along, a freshly installed Windows does not. Without them
# SAIVerse fails at startup with "DLL load failed while importing
# onnxruntime_pybind11_state" (docs/issues/clean_windows_missing_vc_runtime_blocks_startup.md).
#
# This script never fails setup: when it cannot install, it prints what to do.

$ErrorActionPreference = "Stop"

$DownloadUrl = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
$RequiredDlls = @("msvcp140.dll", "msvcp140_1.dll", "vcruntime140.dll", "vcruntime140_1.dll")

function Get-MissingRuntimeDlls {
    $system32 = Join-Path $env:SystemRoot "System32"
    return @($RequiredDlls | Where-Object { -not (Test-Path (Join-Path $system32 $_)) })
}

function Write-ManualInstructions {
    Write-Host "  SAIVerse will not start without it. Install it manually from:"
    Write-Host "    $DownloadUrl"
    Write-Host "  then run setup.bat again."
}

$missing = Get-MissingRuntimeDlls
if ($missing.Count -eq 0) {
    Write-Host "[OK] Visual C++ runtime found"
    exit 0
}

if ($env:PROCESSOR_ARCHITECTURE -ne "AMD64") {
    Write-Host "[WARN] Visual C++ runtime not found, and this is not a 64-bit Intel/AMD Windows ($env:PROCESSOR_ARCHITECTURE)."
    Write-Host "  Automatic installation is only available for x64."
    Write-ManualInstructions
    exit 1
}

Write-Host ""
Write-Host "[SETUP] Visual C++ runtime not found (missing: $($missing -join ', ')). Installing..."
Write-Host "  Windows will ask for permission to make changes. Choose Yes to continue."

$Installer = Join-Path $env:TEMP "saiverse_vc_redist.x64.exe"
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $ProgressPreference = "SilentlyContinue"
    Invoke-WebRequest -Uri $DownloadUrl -OutFile $Installer -UseBasicParsing
} catch {
    Write-Host "[WARN] Could not download the Visual C++ runtime: $_"
    Write-ManualInstructions
    exit 1
}

try {
    # The file comes from the network: run it only if Microsoft signed it.
    $signature = Get-AuthenticodeSignature -FilePath $Installer
    if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notmatch "O=Microsoft Corporation") {
        Write-Host "[WARN] The downloaded installer is not signed by Microsoft (status: $($signature.Status)). It was not run."
        Write-ManualInstructions
        exit 1
    }

    # /passive shows a progress window without asking questions; /norestart
    # leaves the decision to restart with the user. The installer asks for
    # elevation by itself, so this script does not need to run as administrator.
    Start-Process -FilePath $Installer -ArgumentList "/install", "/passive", "/norestart" -Wait
} catch {
    Write-Host "[WARN] The Visual C++ runtime installer did not run: $_"
    Write-ManualInstructions
    exit 1
} finally {
    Remove-Item $Installer -Force -ErrorAction SilentlyContinue
}

# The installer's exit code is not readable when it ran elevated from a
# non-elevated window, so the result is judged by what is on disk.
$missing = Get-MissingRuntimeDlls
if ($missing.Count -eq 0) {
    Write-Host "[OK] Visual C++ runtime installed"
    exit 0
}

Write-Host "[WARN] The Visual C++ runtime is still missing after the installer ran (missing: $($missing -join ', '))."
Write-ManualInstructions
exit 1
