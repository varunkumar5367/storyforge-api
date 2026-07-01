# ─────────────────────────────────────────────────────────────────────────────
# setup_autostart.ps1
# Registers start_storyforge.bat to run automatically on Windows login
# via Windows Task Scheduler.
#
# Run once as Administrator:
#   Right-click setup_autostart.ps1 → "Run as Administrator"
# ─────────────────────────────────────────────────────────────────────────────

$ProjectDir   = "E:\yt\storyforge-api"
$ScriptPath   = Join-Path $ProjectDir "start_storyforge.bat"
$TaskName     = "StoryForge Laptop Listener"
$TaskDesc     = "Starts Ollama and the StoryForge GPU listener dashboard on login"

# ── Validate script exists ────────────────────────────────────────────────────
if (-Not (Test-Path $ScriptPath)) {
    Write-Error "Could not find start_storyforge.bat at: $ScriptPath"
    Write-Error "Make sure you are running this script from the correct project directory."
    exit 1
}

# ── Remove existing task if present (clean re-register) ──────────────────────
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "[StoryForge] Removing existing task '$TaskName'..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# ── Build the action (runs the bat file hidden, no console flash) ─────────────
$Action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/C `"$ScriptPath`""

# ── Trigger: run at user logon ────────────────────────────────────────────────
$Trigger = New-ScheduledTaskTrigger -AtLogOn

# ── Settings: allow running on battery, don't stop on idle, etc. ──────────────
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)   # no time limit

# ── Principal: run as current user ────────────────────────────────────────────
$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Highest

# ── Register the task ─────────────────────────────────────────────────────────
Register-ScheduledTask `
    -TaskName    $TaskName `
    -Description $TaskDesc `
    -Action      $Action `
    -Trigger     $Trigger `
    -Settings    $Settings `
    -Principal   $Principal `
    -Force | Out-Null

Write-Host ""
Write-Host "✅  Task '$TaskName' registered successfully!" -ForegroundColor Green
Write-Host "    It will launch automatically every time you log into Windows." -ForegroundColor Cyan
Write-Host ""
Write-Host "📌 To remove auto-start later, run:" -ForegroundColor Yellow
Write-Host "    Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false" -ForegroundColor Gray
Write-Host ""
Write-Host "📌 To run it manually right now:" -ForegroundColor Yellow
Write-Host "    Start-ScheduledTask -TaskName '$TaskName'" -ForegroundColor Gray
Write-Host ""

# Offer to start it immediately
$answer = Read-Host "Would you like to start the listener right now? (y/n)"
if ($answer -match "^[Yy]") {
    Write-Host "[StoryForge] Launching now..." -ForegroundColor Cyan
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "✅  Listener started! Check your taskbar for the StoryForge dashboard window." -ForegroundColor Green
}
