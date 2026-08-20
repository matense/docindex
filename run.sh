#!/usr/bin/env bash
# DocIndex launcher (Linux/macOS/Git Bash).
# Starts the app on http://localhost:5000 using the venv interpreter
# directly (no "activate" needed).
set -e
cd "$(dirname "$0")"

echo "=== DocIndex ==="

if [ ! -x venv/bin/python ]; then
    echo "ERROR: no virtual environment found - run ./install.sh first."
    exit 1
fi

echo "Starting DocIndex on http://localhost:5000  (Ctrl+C to stop)"
exec venv/bin/python run.py
