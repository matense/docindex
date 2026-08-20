#!/usr/bin/env bash
# DocIndex installer (Linux/macOS/Git Bash).
# Creates the virtualenv, installs dependencies, prepares .env,
# creates the database and the first admin user.
#
# The venv interpreter is always called directly (venv/bin/python) instead of
# relying on "activate": if activation silently failed, pip installed into the
# GLOBAL Python and the app broke — the "venv in the wrong place" bug. Calling
# the interpreter by path makes that impossible.
set -e
cd "$(dirname "$0")"
VENV_PY="venv/bin/python"

echo "=== DocIndex install ==="

# 1. Python check (3.11+)
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found. Install Python 3.11+ first."
    exit 1
fi
if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"; then
    echo "ERROR: Python 3.11+ is required. Found: $(python3 --version 2>&1)"
    exit 1
fi

# 2. Virtual environment (recreate a broken one from a failed attempt)
if [ -d venv ] && [ ! -x "$VENV_PY" ]; then
    echo "-> Found a broken venv folder (no interpreter inside) - recreating it..."
    rm -rf venv
fi
if [ ! -x "$VENV_PY" ]; then
    echo "-> Creating virtual environment in $(pwd)/venv ..."
    python3 -m venv venv
fi
if [ ! -x "$VENV_PY" ]; then
    echo "ERROR: the venv was not created correctly ($VENV_PY missing)."
    exit 1
fi

# 3. Dependencies (always through the venv interpreter)
echo "-> Installing dependencies..."
"$VENV_PY" -m pip install -r requirements.txt

# 4. .env with a generated SECRET_KEY (kept if one already exists)
if [ ! -f .env ]; then
    echo "-> Creating .env with a random SECRET_KEY..."
    "$VENV_PY" -c "import secrets, pathlib; \
        pathlib.Path('.env').write_text( \
        pathlib.Path('.env.example').read_text().replace( \
        'change-me-to-a-long-random-string', secrets.token_hex(32)))"
else
    echo "-> .env already exists, keeping it."
fi

# 5. Database
echo "-> Creating/upgrading the database..."
"$VENV_PY" -m flask --app run.py db upgrade

# 6. First admin user (prompts; or pass args: ./install.sh user email password)
echo "-> Creating the admin user..."
"$VENV_PY" create_admin.py "$@"

echo ""
echo "=== Done! To start the app: ==="
echo "    ./run.sh"
echo "Then open http://localhost:5000"
