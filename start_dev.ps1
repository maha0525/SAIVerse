# SAIVerse Development Startup Script
# Creates session-specific log directory and captures all process outputs

# Create timestamp-based log directory
$LogTimestamp = Get-Date -Format "yyyyMMdd_HHMMss"
$LogDir = "user_data\logs\$LogTimestamp"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
Write-Host "Session logs will be saved to: $LogDir"

# Set environment variable for Backend to use the same log directory
$env:SAIVERSE_SESSION_LOG_DIR = $LogDir

# Start Backend in a new window using the SAIVerse conda environment
# Note: Python logging_config.py already handles backend.log via TeeHandler
Write-Host "Starting Backend..."
Start-Process -FilePath "cmd.exe" -ArgumentList "/k conda activate SAIVerse && python main.py"

# Start SearXNG in a new window with output redirected to log
Write-Host "Starting SearXNG..."
$SearXNGLogPath = Join-Path $LogDir "searxng.log"
Start-Process -FilePath "powershell.exe" -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", "& { Start-Transcript -Path '$SearXNGLogPath' -Force; . scripts\run_searxng_server.ps1; Stop-Transcript }"

# Start Frontend in the current window with output captured
Write-Host "Starting Frontend..."
$FrontendLogPath = Join-Path $LogDir "frontend.log"
Set-Location frontend
npm run dev 2>&1 | Tee-Object -FilePath "..\$FrontendLogPath"
