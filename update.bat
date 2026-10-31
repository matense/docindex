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

if not exist "%VENV_PY%" (
    echo -^> No working venv found - run install.bat first.
    exit /b 1
)

echo -^> Pulling the latest code...
rem Code updates go through dulwich (pure Python, already a dependency) so
rem no git binary is required. Bootstrap it first if this venv lacks it.
"%VENV_PY%" -c "import dulwich" 2>nul
if errorlevel 1 (
    echo -^> Installing dulwich for the updater...
    for /f "tokens=*" %%p in ('findstr /b "dulwich" requirements.txt') do set DULWICH_PIN=%%p
    "%VENV_PY%" -m pip install "%DULWICH_PIN%"
    if errorlevel 1 exit /b 1
)
"%VENV_PY%" tools/git_sync.py pull
if errorlevel 1 exit /b 1

echo -^> Updating dependencies...
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

echo -^> Upgrading the database...
echo    Current version:
"%VENV_PY%" -m flask --app run.py db current
echo    Target version:
"%VENV_PY%" -m flask --app run.py db heads
echo    Large upgrades ^(e.g. the search-index split^) copy data and show
echo    progress per step - allow several minutes, do not interrupt.
"%VENV_PY%" -m flask --app run.py db upgrade heads
if errorlevel 1 (
    echo -^> Standard upgrade failed ^(the database may carry a stamp from the
    echo    first public release^) - repairing the migration stamp...
    "%VENV_PY%" -m flask --app run.py db-repair
    if errorlevel 1 exit /b 1
)

echo.
echo === Done! Restart the server to use the new version: ===
echo     run.bat
endlocal
