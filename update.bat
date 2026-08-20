@echo off
rem DocIndex updater (Windows).
rem Pulls the latest code, updates dependencies and migrates the database
rem in place — your data (instance\, uploads\, .env) is never touched.
rem The venv interpreter is called directly (no "activate") so dependencies
rem can never land in the global Python by accident.
setlocal
cd /d "%~dp0"
set "VENV_PY=venv\Scripts\python.exe"

echo === DocIndex update ===

echo -^> Pulling the latest code...
git pull
if errorlevel 1 exit /b 1

if not exist "%VENV_PY%" (
    echo -^> No working venv found - run install.bat first.
    exit /b 1
)

echo -^> Updating dependencies...
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

echo -^> Upgrading the database...
"%VENV_PY%" -m flask --app run.py db upgrade
if errorlevel 1 exit /b 1

echo.
echo === Done! Restart the server to use the new version: ===
echo     run.bat
endlocal
