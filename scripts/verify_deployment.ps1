# Pre-flight checks before going live. Run from storyforge-api:
#   powershell -ExecutionPolicy Bypass -File scripts\verify_deployment.ps1

param(
    [string]$PublicUrl = "",
    [int]$Port = 8000
)

$ErrorActionPreference = "Continue"
$ApiRoot = Split-Path -Parent $PSScriptRoot
$script:Failed = 0
$script:Warned = 0

function Pass([string]$msg) { Write-Host "[OK]   $msg" -ForegroundColor Green }
function Fail([string]$msg) { Write-Host "[FAIL] $msg" -ForegroundColor Red; $script:Failed++ }
function Warn([string]$msg) { Write-Host "[WARN] $msg" -ForegroundColor Yellow; $script:Warned++ }

Write-Host ""
Write-Host "StoryForge Deployment Verification"
Write-Host "=================================="
Write-Host ""

$python = Join-Path $ApiRoot ".venv\Scripts\python.exe"
if (Test-Path $python) { Pass "Python venv found" } else { Fail "Python venv missing" }

$envFile = Join-Path $ApiRoot ".env"
if (Test-Path $envFile) { Pass ".env exists" } else { Fail ".env missing" }

if (Test-Path $envFile) {
    $envContent = Get-Content $envFile -Raw
    $requiredKeys = @("GROQ_API_KEY", "JWT_SECRET_KEY", "DATABASE_URL", "FRONTEND_URL", "BACKEND_PUBLIC_URL")
    foreach ($key in $requiredKeys) {
        $patternEmpty = "(?m)^" + [regex]::Escape($key) + "=\s*$"
        $patternAny = "(?m)^" + [regex]::Escape($key) + "="
        if ($envContent -match $patternEmpty -or $envContent -notmatch $patternAny) {
            Warn ($key + " is empty or missing")
        } else {
            Pass ($key + " is set")
        }
    }
    if ($envContent -match "(?m)^ENV=production") { Pass "ENV=production" } else { Warn "ENV is not production" }
}

if (Get-Command ffmpeg -ErrorAction SilentlyContinue) { Pass "ffmpeg on PATH" } else { Fail "ffmpeg not on PATH" }
if (Get-Command ffprobe -ErrorAction SilentlyContinue) { Pass "ffprobe on PATH" } else { Fail "ffprobe not on PATH" }
if (Get-Command cloudflared -ErrorAction SilentlyContinue) { Pass "cloudflared on PATH" } else { Warn "cloudflared not installed" }
if (Get-Command nssm -ErrorAction SilentlyContinue) { Pass "nssm on PATH" } else { Warn "nssm not installed" }

$cfConfig = Join-Path $env:USERPROFILE ".cloudflared\config.yml"
if (Test-Path $cfConfig) { Pass "cloudflared config.yml exists" } else { Warn "No cloudflared config.yml yet" }

try {
    $resp = Invoke-WebRequest -Uri ("http://127.0.0.1:" + $Port + "/health") -TimeoutSec 5 -UseBasicParsing
    if ($resp.StatusCode -eq 200) { Pass ("Local API responding on port " + $Port) }
    else { Warn ("Local API returned " + $resp.StatusCode) }
} catch {
    Warn ("Local API not running on port " + $Port)
}

if ($PublicUrl) {
    try {
        $resp = Invoke-WebRequest -Uri ($PublicUrl.TrimEnd("/") + "/health") -TimeoutSec 10 -UseBasicParsing
        if ($resp.StatusCode -eq 200) { Pass ("Public API OK at " + $PublicUrl) }
        else { Warn ("Public API returned " + $resp.StatusCode) }
    } catch {
        Fail ("Public API not reachable at " + $PublicUrl)
    }
}

foreach ($svc in @("StoryForgeAPI", "StoryForgeTunnel")) {
    $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
    if ($s) {
        if ($s.Status -eq "Running") { Pass ("Service " + $svc + " is Running") }
        else { Warn ("Service " + $svc + " status: " + $s.Status) }
    }
}

Write-Host ""
Write-Host ("Summary: " + $script:Failed + " failures, " + $script:Warned + " warnings")
if ($script:Failed -gt 0) { exit 1 }
