@echo off
rem DocIndex launcher (Windows).
rem Starts the app on http://localhost:5000 using the venv interpreter
rem directly (no "activate" needed).
setlocal
cd /d "%~dp0"

echo === DocIndex ===

if not exist venv\Scripts\python.exe (
    echo ERROR: no virtual environment found - run install.bat first.
    exit /b 1
)

echo Starting DocIndex on http://localhost:5000  (Ctrl+C to stop)
venv\Scripts\python.exe run.py
endlocal
