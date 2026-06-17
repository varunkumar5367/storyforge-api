# StoryForge AI — Register auto-start tasks (Task Scheduler)
# Run as Administrator:
#   powershell -ExecutionPolicy Bypass -File install_autostart_tasks.ps1

$ErrorActionPreference = "Stop"

$ApiRoot = Split-Path -Parent $PSScriptRoot
$StartScript = Join-Path $ApiRoot "start_backend.bat"
$LogDir = Join-Path $ApiRoot "logs"

if (-not (Test-Path $StartScript)) {
    Write-Error "start_backend.bat not found at $StartScript"
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# ── Task 1: FastAPI backend ──────────────────────────────────────────────────
$ApiAction = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"$StartScript`"" `
    -WorkingDirectory $ApiRoot

$ApiTrigger = New-ScheduledTaskTrigger -AtLogOn
$ApiSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
    -TaskName "StoryForge-API" `
    -Action $ApiAction `
    -Trigger $ApiTrigger `
    -Settings $ApiSettings `
    -Description "StoryForge FastAPI backend (uvicorn :8000)" `
    -Force | Out-Null

Write-Host "Registered: StoryForge-API"

# ── Task 2: Cloudflare Tunnel (30s delay for API warmup) ─────────────────────
$TunnelCmd = @"
`$null = Start-Sleep -Seconds 30
cloudflared tunnel run storyforge-api *>> `"$LogDir\tunnel.log`"
"@

$TunnelScript = Join-Path $LogDir "run_tunnel.ps1"
Set-Content -Path $TunnelScript -Value $TunnelCmd -Encoding UTF8

$TunnelAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$TunnelScript`"" `
    -WorkingDirectory $ApiRoot

$TunnelTrigger = New-ScheduledTaskTrigger -AtLogOn
$TunnelSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 2)

Register-ScheduledTask `
    -TaskName "StoryForge-CloudflareTunnel" `
    -Action $TunnelAction `
    -Trigger $TunnelTrigger `
    -Settings $TunnelSettings `
    -Description "Cloudflare Tunnel for StoryForge API" `
    -Force | Out-Null

Write-Host "Registered: StoryForge-CloudflareTunnel (30s delay after logon)"
Write-Host ""
Write-Host "Done. Tasks start at next user logon."
Write-Host "Test now:"
Write-Host "  Start-ScheduledTask -TaskName StoryForge-API"
Write-Host "  Start-ScheduledTask -TaskName StoryForge-CloudflareTunnel"
