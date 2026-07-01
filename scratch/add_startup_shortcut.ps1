$startupFolder = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupFolder 'StoryForge Listener.lnk'

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($shortcutPath)
$Shortcut.TargetPath = 'E:\yt\storyforge-api\start_storyforge.bat'
$Shortcut.WorkingDirectory = 'E:\yt\storyforge-api'
$Shortcut.Description = 'StoryForge Laptop Listener - GPU AI generation daemon'
$Shortcut.WindowStyle = 7
$Shortcut.Save()

Write-Host ('Shortcut created at: ' + $shortcutPath) -ForegroundColor Green
Write-Host 'StoryForge will now auto-start on every Windows login.' -ForegroundColor Cyan
