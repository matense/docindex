#!/usr/bin/env bash
# DocIndex updater (Linux/macOS/Git Bash).
# Pulls the latest code, updates dependencies and migrates the database
# in place — your data (instance/, uploads/, .env) is never touched.
# The venv interpreter is called directly (no "activate") so dependencies
# can never land in the global Python by accident.
set -e
cd "$(dirname "$0")"
VENV_PY="venv/bin/python"

echo "=== DocIndex update ==="

echo "-> Pulling the latest code..."
git pull

if [ ! -x "$VENV_PY" ]; then
    echo "-> No working venv found — run ./install.sh first."
    exit 1
fi

echo "-> Updating dependencies..."
"$VENV_PY" -m pip install -r requirements.txt

echo "-> Upgrading the database..."
"$VENV_PY" -m flask --app run.py db upgrade

echo ""
echo "=== Done! Restart the server to use the new version: ==="
echo "    ./run.sh"
