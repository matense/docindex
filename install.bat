@echo off
rem DocIndex installer (Windows).
rem Creates the virtualenv, installs dependencies, prepares .env,
rem creates the database and the first admin user.
setlocal
cd /d "%~dp0"

echo === DocIndex install ===

rem 1. Python check
where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: python not found. Install Python 3.11+ first.
    exit /b 1
)

rem 2. Virtual environment
if not exist venv (
    echo -^> Creating virtual environment...
    python -m venv venv
)

rem 3. Dependencies
echo -^> Installing dependencies...
venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

rem 4. .env with a generated SECRET_KEY (kept if one already exists)
if not exist .env (
    echo -^> Creating .env with a random SECRET_KEY...
    venv\Scripts\python.exe -c "import secrets, pathlib; pathlib.Path('.env').write_text(pathlib.Path('.env.example').read_text().replace('change-me-to-a-long-random-string', secrets.token_hex(32)))"
) else (
    echo -^> .env already exists, keeping it.
)

rem 5. Database
echo -^> Creating/upgrading the database...
venv\Scripts\python.exe -m flask --app run.py db upgrade
if errorlevel 1 exit /b 1

rem 6. First admin user (prompts; or pass args: install.bat user email password)
echo -^> Creating the admin user...
venv\Scripts\python.exe create_admin.py %*

echo.
echo === Done! Start the app with:  python run.py  (http://localhost:5000) ===
endlocal
