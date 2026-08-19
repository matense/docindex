@echo off
rem DocIndex launcher (Windows).
rem Activates the virtualenv and starts the app on http://localhost:5000
setlocal
cd /d "%~dp0"

echo === DocIndex ===

if not exist venv (
    echo ERROR: no virtual environment found - run install.bat first.
    exit /b 1
)
call venv\Scripts\activate.bat

echo Starting DocIndex on http://localhost:5000  (Ctrl+C to stop)
python run.py
endlocal
