# Start Backend in a new window using the SAIVerse conda environment
Write-Host "Starting Backend..."
Start-Process -FilePath "cmd.exe" -ArgumentList "/k conda activate SAIVerse && python main.py"

# Start SearXNG in a new window
Write-Host "Starting SearXNG..."
Start-Process -FilePath "powershell.exe" -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-File", "scripts\run_searxng_server.ps1"

# Start Frontend in the current window
Write-Host "Starting Frontend..."
Set-Location frontend
npm run dev
