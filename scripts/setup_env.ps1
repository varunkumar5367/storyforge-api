# Adds missing keys from .env.example into .env without overwriting existing values.
# Run from storyforge-api folder:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_env.ps1

$ErrorActionPreference = "Stop"
$ApiRoot = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $ApiRoot ".env"
$ExampleFile = Join-Path $ApiRoot ".env.example"

if (-not (Test-Path $EnvFile)) {
    Copy-Item $ExampleFile $EnvFile
    Write-Host "Created .env from .env.example"
    exit 0
}

$existing = @{}
Get-Content $EnvFile | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') {
        $existing[$matches[1]] = $true
    }
}

$toAdd = @()
Get-Content $ExampleFile | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') {
        $key = $matches[1]
        if (-not $existing.ContainsKey($key)) {
            $toAdd += $_
        }
    }
}

if ($toAdd.Count -eq 0) {
    Write-Host ".env already has all keys from .env.example"
    exit 0
}

Add-Content -Path $EnvFile -Value "`n# --- added by setup_env.ps1 ---"
$toAdd | ForEach-Object { Add-Content -Path $EnvFile -Value $_ }
Write-Host "Added $($toAdd.Count) missing key(s) to .env:"
$toAdd | ForEach-Object { Write-Host "  $_" }
