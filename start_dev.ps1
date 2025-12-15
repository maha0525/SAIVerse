# Start Backend in a new window using the SAIVerse conda environment
Write-Host "Starting Backend..."
Start-Process -FilePath "cmd.exe" -ArgumentList "/k conda activate SAIVerse && python main.py"

# Start Frontend in the current window
Write-Host "Starting Frontend..."
Set-Location frontend
npm run dev
