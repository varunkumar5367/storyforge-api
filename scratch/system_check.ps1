$ErrorActionPreference = "Continue"

# Check 1: Ollama running?
$ollama = Get-Process -Name "ollama" -ErrorAction SilentlyContinue
if ($ollama) {
    Write-Host "OLLAMA: RUNNING (PID $($ollama.Id))"
} else {
    Write-Host "OLLAMA: NOT RUNNING"
}

# Check 2: Ollama API responding?
try {
    $r = Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -Method GET -TimeoutSec 5
    $modelNames = ($r.models | ForEach-Object { $_.name }) -join ", "
    Write-Host "OLLAMA_API: OK - models: $modelNames"
} catch {
    Write-Host "OLLAMA_API: FAILED - $($_.Exception.Message)"
}

# Check 3: cloudflared installed?
$cf = Get-Command cloudflared -ErrorAction SilentlyContinue
if ($cf) {
    Write-Host "CLOUDFLARED: INSTALLED at $($cf.Source)"
} else {
    Write-Host "CLOUDFLARED: NOT FOUND"
}

# Check 4: pythonw.exe in venv?
$pw = Test-Path "E:\yt\storyforge-api\.venv\Scripts\pythonw.exe"
if ($pw) { Write-Host "PYTHONW: EXISTS" } else { Write-Host "PYTHONW: MISSING" }

# Check 5: python in venv?
$pyv = & "E:\yt\storyforge-api\.venv\Scripts\python.exe" --version 2>&1
Write-Host "PYTHON_VENV: $pyv"

# Check 6: ffmpeg?
try {
    $ffv = (ffmpeg -version 2>&1) | Select-Object -First 1
    Write-Host "FFMPEG: $ffv"
} catch {
    Write-Host "FFMPEG: NOT FOUND"
}

# Check 7: Startup shortcut exists?
$lnkPath = "C:\Users\varun\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\StoryForge Listener.lnk"
if (Test-Path $lnkPath) { Write-Host "STARTUP_SHORTCUT: EXISTS" } else { Write-Host "STARTUP_SHORTCUT: MISSING" }

# Check 8: CUDA available?
$cudaCheck = & "E:\yt\storyforge-api\.venv\Scripts\python.exe" -c "import torch; print('CUDA:', torch.cuda.is_available(), '| Device:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'None')" 2>&1
Write-Host "TORCH_CUDA: $cudaCheck"

# Check 9: Database URL set correctly?
$envContent = Get-Content "E:\yt\storyforge-api\.env" | Where-Object { $_ -match "^DATABASE_URL" }
Write-Host "DATABASE_URL_LINE: $envContent"

# Check 10: Render reachable?
try {
    $rend = Invoke-RestMethod -Uri "https://storyforge-api-39h2.onrender.com/" -TimeoutSec 15
    Write-Host "RENDER_BACKEND: OK - status=$($rend.status) version=$($rend.version)"
} catch {
    Write-Host "RENDER_BACKEND: FAILED - $($_.Exception.Message)"
}
