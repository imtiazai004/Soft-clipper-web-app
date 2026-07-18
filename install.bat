@echo off
REM One-time dev setup: Python env + Python deps + frontend build.
cd /d "%~dp0"
echo ============================================
echo   Soft Clipper - Dev Setup
echo ============================================

where uv >nul 2>nul
if %errorlevel% neq 0 (
    echo Installing uv package manager...
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

echo Creating Python environment...
uv venv .venv --python 3.12
echo Installing Python dependencies...
uv pip install -r requirements.txt --python .venv\Scripts\python.exe

echo Building frontend...
cd frontend
call npm install
call npm run build
cd ..

echo.
echo Done! Run the app with run.bat
pause
