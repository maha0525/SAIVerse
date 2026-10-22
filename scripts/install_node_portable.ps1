# Install the portable Node.js (ZIP) into .node/, or replace an existing one
# whose version differs from the pinned one.
#
# Called from setup.bat (no system Node.js and no winget, or a .node/ already
# present), from scripts/update_engine.py on every update of a checkout that
# has a .node/, and from start.bat / start-dev.bat on every start (only reads
# the version when it already matches) -- docs/intent/dependency_management.md
# section 2-6.
#
# A failed download, a checksum mismatch, a broken archive or a .node/ that is
# in use leaves the existing .node/ exactly as it was and exits 1. The new
# version is unpacked next to .node/ (same volume) and swapped in by renaming,
# so .node/ is never half-written.
#
# Exit codes: 0 = .node/ holds the pinned version (already, or now). 1 = it
# could not be installed or replaced; whatever .node/ held before is intact.
#
# -ArchivePath installs from a local ZIP instead of downloading (tests, offline
# installs). -NodeVersion / -ExpectedSha256 exist for the tests and must be
# changed together; the pinned pair below is what users get.

param(
    [string]$ProjectRoot = "",
    [string]$ArchivePath = "",
    [string]$NodeVersion = "24.22.1",
    # sha256 of node-v24.22.1-win-x64.zip from https://nodejs.org/dist/v24.22.1/SHASUMS256.txt
    [string]$ExpectedSha256 = "d2046d28c2c3dcd211ac5145c0bf30e2047ef9dfb306693c8c6e2eccd8b95fa5"
)

$ErrorActionPreference = "Stop"
# Windows PowerShell 5.1 redraws the progress bar per chunk, which makes a
# 30 MB download take minutes.
$ProgressPreference = "SilentlyContinue"

$Arch = "x64"
if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}
# .NET resolves relative paths against the process directory, not PowerShell's.
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath
$NodeDir = Join-Path $ProjectRoot ".node"
# Both live beside .node/ so the swap is a rename on the same volume.
$StagingDir = Join-Path $ProjectRoot ".node.new"
$RetiredDir = Join-Path $ProjectRoot ".node.old"
$Expected = "v$NodeVersion"
$ZipName = "node-v${NodeVersion}-win-${Arch}"
$ZipUrl = "https://nodejs.org/dist/v${NodeVersion}/${ZipName}.zip"
$DownloadedZip = $null

function Get-NodeVersion([string]$Dir) {
    # The version node.exe in $Dir reports, or $null when it is missing or
    # does not run (a broken .node/ counts as "differs" and is replaced).
    $exe = Join-Path $Dir "node.exe"
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { return $null }
    # Under "Stop", Windows PowerShell 5.1 turns any stderr line of a native
    # command into a terminating error even when redirected, so a working
    # node that prints one warning (NODE_OPTIONS, for instance) would read as
    # unreadable and be downloaded again on every update. Scoped to here.
    $ErrorActionPreference = "Continue"
    try {
        $out = & $exe --version 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return ("$out").Trim()
    } catch {
        return $null
    }
}

function Rename-Folder([string]$From, [string]$To) {
    # A plain rename: it moves the whole folder or nothing. Move-Item is not
    # used because, when the rename is refused (a file in the folder is in
    # use), it falls back to moving the files one by one and stops at the
    # locked one -- leaving .node/ half emptied (found by the tests).
    [System.IO.Directory]::Move($From, $To)
}

function Remove-Leftover([string]$Dir) {
    if (Test-Path -LiteralPath $Dir) {
        Remove-Item -LiteralPath $Dir -Recurse -Force
    }
}

try {
    # A previous run that died between its two renames left .node/ moved
    # aside and nothing in its place: put it back before judging anything.
    if ((Test-Path -LiteralPath $RetiredDir) -and -not (Test-Path -LiteralPath $NodeDir)) {
        Rename-Folder $RetiredDir $NodeDir
    }
    # Any other leftover is garbage. Both must be gone before the swap, which
    # renames onto these names.
    Remove-Leftover $RetiredDir
    Remove-Leftover $StagingDir

    $current = Get-NodeVersion $NodeDir
    if ($current -eq $Expected) {
        Write-Host "[OK] Node.js portable $current is the pinned version"
        exit 0
    }
    if (Test-Path -LiteralPath $NodeDir) {
        $shown = if ($current) { $current } else { "an unreadable version" }
        Write-Host "[UPDATE] Replacing Node.js portable $shown with $Expected ..."
    } else {
        Write-Host "[SETUP] Installing Node.js portable $Expected ..."
    }

    if ($ArchivePath) {
        $Zip = $ArchivePath
    } else {
        # PowerShell 5.1 on older .NET does not offer TLS 1.2 by default.
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $DownloadedZip = Join-Path $env:TEMP "${ZipName}-$PID.zip"
        Write-Host "[SETUP] Downloading $ZipUrl"
        Invoke-WebRequest -Uri $ZipUrl -OutFile $DownloadedZip -UseBasicParsing
        $Zip = $DownloadedZip
    }

    $actual = (Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $ExpectedSha256.ToLowerInvariant()) {
        throw "Checksum mismatch for ${ZipName}.zip: expected $ExpectedSha256, got $actual"
    }

    Expand-Archive -LiteralPath $Zip -DestinationPath $StagingDir
    $Inner = Join-Path $StagingDir $ZipName
    $staged = Get-NodeVersion $Inner
    if ($staged -ne $Expected) {
        throw "The unpacked node.exe reports '$staged', expected $Expected"
    }

    # The swap. Renaming .node/ fails while anything runs from it (the
    # frontend, an MCP server), which is the safe outcome: nothing changed.
    $hadOld = Test-Path -LiteralPath $NodeDir
    if ($hadOld) {
        Rename-Folder $NodeDir $RetiredDir
    }
    try {
        Rename-Folder $Inner $NodeDir
    } catch {
        if ($hadOld) { Rename-Folder $RetiredDir $NodeDir }
        throw
    }
} catch {
    Write-Host "[ERROR] Node.js portable $Expected could not be installed: $_"
    # The swap's own undo failed too: one more try, so the caller is not left
    # without a .node (the next run would also put it back first).
    if ((Test-Path -LiteralPath $RetiredDir) -and -not (Test-Path -LiteralPath $NodeDir)) {
        try { Rename-Folder $RetiredDir $NodeDir } catch { }
    }
    if (Test-Path -LiteralPath $NodeDir) {
        Write-Host "[ERROR] The existing .node/ was left as it was."
    }
    try { Remove-Leftover $StagingDir } catch { }
    if ($DownloadedZip) { Remove-Item -LiteralPath $DownloadedZip -Force -ErrorAction SilentlyContinue }
    exit 1
}

# The new version is in place; what follows is cleanup only, and whatever it
# cannot remove now the next run removes first.
if ($DownloadedZip) { Remove-Item -LiteralPath $DownloadedZip -Force -ErrorAction SilentlyContinue }
foreach ($leftover in @($StagingDir, $RetiredDir)) {
    try {
        Remove-Leftover $leftover
    } catch {
        Write-Host "[WARN] Could not remove $leftover yet; the next run removes it: $_"
    }
}
Write-Host "[OK] Node.js $(Get-NodeVersion $NodeDir) installed to .node/"
exit 0
