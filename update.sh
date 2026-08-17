#!/usr/bin/env bash
# DocIndex updater (Linux/macOS/Git Bash).
# Pulls the latest code, updates dependencies and migrates the database
# in place — your data (instance/, uploads/, .env) is never touched.
set -e
cd "$(dirname "$0")"

echo "=== DocIndex update ==="

echo "-> Pulling the latest code..."
git pull

if [ -d venv ]; then
    # shellcheck disable=SC1091
    source venv/bin/activate
    echo "-> Updating dependencies..."
    pip install -r requirements.txt
    echo "-> Upgrading the database..."
    python -m flask --app run.py db upgrade
else
    echo "-> No venv found — run ./install.sh first."
    exit 1
fi

echo ""
echo "=== Done! Restart the server (python run.py) to use the new version. ==="
