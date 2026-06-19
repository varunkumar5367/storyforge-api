@echo off
cd /d "%~dp0"
echo Starting StoryForge Laptop Listener Daemon...
.venv\Scripts\python.exe laptop_listener.py
pause
