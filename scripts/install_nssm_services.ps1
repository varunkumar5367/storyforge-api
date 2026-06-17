# StoryForge AI — Install Windows services via NSSM
#
# Prerequisites:
#   winget install NSSM.NSSM
#   winget install Cloudflare.cloudflared
#   cloudflared tunnel login   (once, as YOUR user)
#   cloudflared tunnel create storyforge-api
#
# Run as Administrator:
#   powershell -ExecutionPolicy Bypass -File install_nssm_services.ps1
#
# Optional parameters:
#   -TunnelName storyforge-api
#   -Port 8000

param(
    [string]$TunnelName = "storyforge-api",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

$ApiRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $ApiRoot ".venv\Scripts\python.exe"
$LogDir = Join-Path $ApiRoot "logs"
$Nssm = (Get-Command nssm -ErrorAction SilentlyContinue).Source

if (-not $Nssm) {
    Write-Error "nssm not found. Install with: winget install NSSM.NSSM"
}
if (-not (Test-Path $VenvPython)) {
    Write-Error "Python venv not found at $VenvPython — run: python -m venv .venv; pip install -r requirements.txt"
}
if (-not (Test-Path (Join-Path $ApiRoot ".env"))) {
    Write-Warning ".env not found at $ApiRoot\.env — copy .env.example and configure before starting services."
}

$CloudflaredConfig = Join-Path $env:USERPROFILE ".cloudflared\config.yml"
if (-not (Test-Path $CloudflaredConfig)) {
    Write-Warning "Cloudflared config not found at $CloudflaredConfig — create tunnel first (see docs/CLOUDFLARE_TUNNEL_SETUP.md)."
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Services must run as YOUR Windows user so cloudflared can read ~/.cloudflared credentials.
$CurrentUser = "$env:USERDOMAIN\$env:USERNAME"
Write-Host ""
Write-Host "Services will run as: $CurrentUser"
Write-Host "(Required — cloudflared credentials live in $env:USERPROFILE\.cloudflared)"
Write-Host ""
$Credential = Get-Credential -UserName $CurrentUser -Message "Enter your Windows password (stored for auto-start at boot)"

function Install-StoryForgeService {
    param(
        [string]$Name,
        [string]$Executable,
        [string]$Arguments,
        [string]$StdoutLog,
        [string]$StderrLog,
        [string]$DependsOn = $null,
        [int]$RestartDelayMs = 5000
    )

    # Remove existing service if present
    $existing = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "Removing existing service: $Name"
        & $Nssm stop $Name confirm 2>$null
        & $Nssm remove $Name confirm
    }

    & $Nssm install $Name $Executable
    & $Nssm set $Name AppDirectory $ApiRoot
    & $Nssm set $Name AppParameters $Arguments
    & $Nssm set $Name AppStdout $StdoutLog
    & $Nssm set $Name AppStderr $StderrLog
    & $Nssm set $Name AppStdoutCreationDisposition 4
    & $Nssm set $Name AppStderrCreationDisposition 4
    & $Nssm set $Name AppRotateFiles 1
    & $Nssm set $Name AppRotateBytes 10485760
    & $Nssm set $Name Start SERVICE_AUTO_START
    & $Nssm set $Name AppExit Default Restart
    & $Nssm set $Name AppRestartDelay $RestartDelayMs
    & $Nssm set $Name ObjectName $Credential.UserName $Credential.GetNetworkCredential().Password

    if ($DependsOn) {
        & $Nssm set $Name DependOnService $DependsOn
    }

    Write-Host "Installed: $Name"
}

# ── Service 1: FastAPI ───────────────────────────────────────────────────────
$ApiArgs = "run_server.py"
Install-StoryForgeService `
    -Name "StoryForgeAPI" `
    -Executable $VenvPython `
    -Arguments $ApiArgs `
    -StdoutLog (Join-Path $LogDir "service_api.log") `
    -StderrLog (Join-Path $LogDir "service_api_err.log") `
    -RestartDelayMs 5000

# ── Service 2: Cloudflare Tunnel ─────────────────────────────────────────────
$Cloudflared = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $Cloudflared) {
    Write-Warning "cloudflared not on PATH — tunnel service skipped. Install: winget install Cloudflare.cloudflared"
} else {
    $TunnelArgs = "tunnel run $TunnelName"
    Install-StoryForgeService `
        -Name "StoryForgeTunnel" `
        -Executable $Cloudflared `
        -Arguments $TunnelArgs `
        -StdoutLog (Join-Path $LogDir "service_tunnel.log") `
        -StderrLog (Join-Path $LogDir "service_tunnel_err.log") `
        -DependsOn "StoryForgeAPI" `
        -RestartDelayMs 15000

    Write-Host "StoryForgeTunnel depends on StoryForgeAPI (starts after API service)"
}

Write-Host ""
Write-Host "============================================"
Write-Host " NSSM services installed."
Write-Host "============================================"
Write-Host ""
Write-Host "Start now:"
Write-Host "  nssm start StoryForgeAPI"
Write-Host "  Start-Sleep -Seconds 5"
Write-Host "  nssm start StoryForgeTunnel"
Write-Host ""
Write-Host "Check status:"
Write-Host "  Get-Service StoryForgeAPI, StoryForgeTunnel"
Write-Host "  curl http://127.0.0.1:$Port/health"
Write-Host ""
Write-Host "Logs:"
Write-Host "  $LogDir\service_api.log"
Write-Host "  $LogDir\service_tunnel.log"
Write-Host ""
Write-Host "Uninstall:"
Write-Host "  powershell -ExecutionPolicy Bypass -File uninstall_nssm_services.ps1"
