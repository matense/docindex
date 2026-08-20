@echo off
rem DocIndex installer (Windows).
rem Creates the virtualenv, installs dependencies, prepares .env,
rem creates the database and the first admin user.
rem
rem The venv interpreter is always called directly (venv\Scripts\python.exe)
rem instead of relying on "activate": if activation silently failed, pip
rem installed into the GLOBAL Python and the app broke — the "venv in the
rem wrong place" bug. Calling the interpreter by path makes that impossible.
setlocal
cd /d "%~dp0"
set "VENV_PY=venv\Scripts\python.exe"

echo === DocIndex install ===

rem 1. Python check (3.11+)
where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: python not found. Install Python 3.11+ first.
    exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo ERROR: Python 3.11+ is required. Found:
    python --version
    exit /b 1
)

rem 2. Virtual environment (recreate a broken one from a failed attempt)
if exist venv\ if not exist "%VENV_PY%" (
    echo -^> Found a broken venv folder ^(no interpreter inside^) - recreating it...
    rmdir /s /q venv
)
if not exist "%VENV_PY%" (
    echo -^> Creating virtual environment in %CD%\venv ...
    python -m venv venv
    if errorlevel 1 exit /b 1
)
if not exist "%VENV_PY%" (
    echo ERROR: the venv was not created correctly ^(%VENV_PY% missing^).
    exit /b 1
)

rem 3. Dependencies (always through the venv interpreter)
echo -^> Installing dependencies...
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

rem 4. .env with a generated SECRET_KEY (kept if one already exists)
if not exist .env (
    echo -^> Creating .env with a random SECRET_KEY...
    "%VENV_PY%" -c "import secrets, pathlib; pathlib.Path('.env').write_text(pathlib.Path('.env.example').read_text().replace('change-me-to-a-long-random-string', secrets.token_hex(32)))"
) else (
    echo -^> .env already exists, keeping it.
)

rem 5. Database
echo -^> Creating/upgrading the database...
"%VENV_PY%" -m flask --app run.py db upgrade
if errorlevel 1 exit /b 1

rem 6. First admin user (prompts; or pass args: install.bat user email password)
echo -^> Creating the admin user...
"%VENV_PY%" create_admin.py %*

echo.
echo === Done! To start the app: ===
echo     run.bat
echo Then open http://localhost:5000
endlocal
