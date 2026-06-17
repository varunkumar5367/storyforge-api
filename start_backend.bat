@echo off
setlocal

REM StoryForge AI - Windows backend launcher

cd /d "%~dp0"

if not exist "logs" mkdir logs

echo [%date% %time%] StoryForge backend startup >> logs\backend.log

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: Virtual environment not found at .venv
    echo Run: python -m venv .venv
    echo Then: .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo ERROR: ffmpeg not found on PATH
    echo Install from https://ffmpeg.org/download.html
    exit /b 1
)

where ffprobe >nul 2>&1
if errorlevel 1 (
    echo ERROR: ffprobe not found on PATH
    echo Install FFmpeg - it includes ffprobe
    exit /b 1
)

set LOGFILE=logs\uvicorn.log

echo.
echo ============================================
echo  StoryForge AI Backend
echo  Log file: %LOGFILE%
echo ============================================
echo.

ffmpeg -version 2>nul | findstr /i "ffmpeg version"
echo.

if not exist ".env" (
    echo WARNING: .env file not found
    echo Copy .env.example to .env and configure it
)

echo Starting backend on 0.0.0.0:8000 ...
echo Logs also written to storyforge.log
echo Press Ctrl+C to stop.
echo.

.venv\Scripts\python.exe run_server.py

endlocal
