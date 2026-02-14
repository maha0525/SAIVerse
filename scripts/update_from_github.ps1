# SAIVerse - GitHub からコードをダウンロードして更新するスクリプト
# git がインストールされていない環境用
#
# 使い方:
#   powershell -ExecutionPolicy Bypass -File scripts\update_from_github.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\update_from_github.ps1 -Branch develop

param(
    [string]$Branch = "main",
    [string]$Repo = "maha0525/SAIVerse"
)

$ErrorActionPreference = "Stop"

# プロジェクトルートを特定 (scripts/ の親ディレクトリ)
$ProjectRoot = Split-Path -Parent $PSScriptRoot

Write-Host ""
Write-Host "========================================"
Write-Host "  SAIVerse Code Update (GitHub ZIP)"
Write-Host "========================================"
Write-Host ""
Write-Host "[INFO] Repository: $Repo"
Write-Host "[INFO] Branch:     $Branch"
Write-Host "[INFO] Target:     $ProjectRoot"
Write-Host ""

# --- 1. ダウンロード ---
$zipUrl = "https://github.com/$Repo/archive/refs/heads/$Branch.zip"
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$tempDir = Join-Path $env:TEMP "saiverse_update_$timestamp"
$zipPath = "$tempDir.zip"

Write-Host "[UPDATE] $zipUrl からダウンロード中..."
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
} catch {
    Write-Host "[ERROR] ダウンロードに失敗しました: $_" -ForegroundColor Red
    Write-Host "  URL: $zipUrl"
    Write-Host "  ネットワーク接続とリポジトリ名を確認してください。"
    exit 1
}
Write-Host "[OK] ダウンロード完了 ($([math]::Round((Get-Item $zipPath).Length / 1MB, 1)) MB)"

# --- 2. 展開 ---
Write-Host "[UPDATE] 一時ディレクトリに展開中..."
try {
    Expand-Archive -Path $zipPath -DestinationPath $tempDir -Force
} catch {
    Write-Host "[ERROR] ZIP の展開に失敗しました: $_" -ForegroundColor Red
    Remove-Item -Path $zipPath -Force -ErrorAction SilentlyContinue
    exit 1
}

# GitHub の zip は "SAIVerse-{branch}/" というサブディレクトリに展開される
$extractedDir = Get-ChildItem -Path $tempDir -Directory | Select-Object -First 1
if (-not $extractedDir) {
    Write-Host "[ERROR] 展開されたディレクトリが見つかりません。" -ForegroundColor Red
    Remove-Item -Path $zipPath -Force -ErrorAction SilentlyContinue
    Remove-Item -Path $tempDir -Recurse -Force -ErrorAction SilentlyContinue
    exit 1
}

Write-Host "[OK] 展開完了: $($extractedDir.Name)"

# --- 3. ファイルをコピー ---
# GitHub の zip には gitignore 対象のファイルは含まれないため、
# user_data/, .venv/, .env, node_modules/ 等は上書きされない。
# そのまま全ファイルをコピーすれば安全。
Write-Host "[UPDATE] ファイルを更新中..."

$sourceDir = $extractedDir.FullName
$fileCount = 0
$dirCount = 0

Get-ChildItem -Path $sourceDir -Recurse -File | ForEach-Object {
    $relativePath = $_.FullName.Substring($sourceDir.Length + 1)
    $destPath = Join-Path $ProjectRoot $relativePath
    $destDir = Split-Path $destPath -Parent

    if (-not (Test-Path $destDir)) {
        New-Item -ItemType Directory -Path $destDir -Force | Out-Null
        $script:dirCount++
    }

    Copy-Item -Path $_.FullName -Destination $destPath -Force
    $script:fileCount++
}

Write-Host "[OK] $fileCount 個のファイルを更新しました ($dirCount 個の新しいディレクトリ)"

# --- 4. クリーンアップ ---
Write-Host "[UPDATE] 一時ファイルを削除中..."
Remove-Item -Path $zipPath -Force -ErrorAction SilentlyContinue
Remove-Item -Path $tempDir -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "========================================"
Write-Host "  コードの更新が完了しました"
Write-Host "========================================"
Write-Host ""
Write-Host "以下のファイルは保護されています (上書きされません):"
Write-Host "  - .env           (API キー等の設定)"
Write-Host "  - user_data\     (データベース、カスタムツール等)"
Write-Host "  - .venv\         (Python 仮想環境)"
Write-Host "  - frontend\node_modules\  (npm パッケージ)"
Write-Host ""
Write-Host "続けて update.bat を実行してパッケージとデータベースを更新してください。"
Write-Host ""
