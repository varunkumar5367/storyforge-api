# Remove StoryForge NSSM services — run as Administrator

$Nssm = (Get-Command nssm -ErrorAction SilentlyContinue).Source
if (-not $Nssm) { Write-Error "nssm not found" }

foreach ($name in @("StoryForgeTunnel", "StoryForgeAPI")) {
    & $Nssm stop $name 2>$null
    & $Nssm remove $name confirm
    Write-Host "Removed: $name"
}
