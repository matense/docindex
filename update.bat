@echo off
rem DocIndex updater (Windows).
rem Pulls the latest code, updates dependencies and migrates the database
rem in place — your data (instance\, uploads\, .env) is never touched.
setlocal
cd /d "%~dp0"

echo === DocIndex update ===

echo -^> Pulling the latest code...
git pull
if errorlevel 1 exit /b 1

if exist venv (
    call venv\Scripts\activate.bat
    echo -^> Updating dependencies...
    python -m pip install -r requirements.txt
    if errorlevel 1 exit /b 1
    echo -^> Upgrading the database...
    python -m flask --app run.py db upgrade
    if errorlevel 1 exit /b 1
) else (
    echo -^> No venv found — run install.bat first.
    exit /b 1
)

echo.
echo === Done! Restart the server to use the new version: ===
echo     venv\Scripts\activate
echo     python run.py
endlocal
