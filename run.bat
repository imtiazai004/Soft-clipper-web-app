@echo off
REM Run the app from source (for development / testing changes).
cd /d "%~dp0"
.venv\Scripts\python.exe launcher.py
pause
