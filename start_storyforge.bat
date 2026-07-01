@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM start_storyforge.bat
REM Starts Ollama + StoryForge Laptop Listener on Windows login.
REM Registered by setup_autostart.ps1 — do not move this file.
REM ─────────────────────────────────────────────────────────────────────────────

SET PROJECT_DIR=E:\yt\storyforge-api
SET VENV_PYTHON=%PROJECT_DIR%\.venv\Scripts\pythonw.exe

REM ── 1. Start Ollama in background (if not already running) ──────────────────
tasklist /FI "IMAGENAME eq ollama.exe" 2>NUL | find /I /N "ollama.exe" >NUL
IF NOT ERRORLEVEL 1 (
    echo [StoryForge] Ollama already running, skipping start.
) ELSE (
    echo [StoryForge] Starting Ollama...
    start "" /B ollama serve
    timeout /T 5 /NOBREAK >NUL
)

REM ── 2. Start the Laptop Listener GUI ────────────────────────────────────────
echo [StoryForge] Launching listener dashboard...
start "" "%VENV_PYTHON%" "%PROJECT_DIR%\laptop_listener.py"
