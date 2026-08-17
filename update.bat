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
    echo -^> Updating dependencies...
    venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 exit /b 1
    echo -^> Upgrading the database...
    venv\Scripts\python.exe -m flask --app run.py db upgrade
    if errorlevel 1 exit /b 1
) else (
    echo -^> No venv found — run install.bat first.
    exit /b 1
)

echo.
echo === Done! Restart the server (python run.py) to use the new version. ===
endlocal
