#!/usr/bin/env bash
# DocIndex updater (Linux/macOS/Git Bash).
# Pulls the latest code, updates dependencies and migrates the database
# in place — your data (instance/, uploads/, .env) is never touched.
# The venv interpreter is called directly (no "activate") so dependencies
# can never land in the global Python by accident.
set -e
cd "$(dirname "$0")"
VENV_PY="venv/bin/python"

# All output is mirrored to logs/update.log (previous run kept as
# .prev) via tools/tee.py - send that file when reporting a failure.
if [ -z "$DOCINDEX_LOGGING" ]; then
    export DOCINDEX_LOGGING=1
    exec "$VENV_PY" tools/tee.py logs/update.log "$0" "$@"
fi

echo "=== DocIndex update ==="

if [ ! -x "$VENV_PY" ]; then
    echo "-> No working venv found — run ./install.sh first."
    exit 1
fi

echo "-> Pulling the latest code..."
# Code updates go through dulwich (pure Python, already a dependency) so
# no git binary is required. Bootstrap it first if this venv lacks it.
if ! "$VENV_PY" -c "import dulwich" 2>/dev/null; then
    echo "-> Installing dulwich for the updater..."
    DULWICH_PIN=$(grep -m1 -E "^dulwich" requirements.txt || true)
    if [ -n "$DULWICH_PIN" ]; then
        "$VENV_PY" -m pip install "$DULWICH_PIN"
    else
        # Old checkout predates the dulwich pin - install unpinned.
        "$VENV_PY" -m pip install dulwich
    fi
    if ! "$VENV_PY" -c "import dulwich" 2>/dev/null; then
        echo "ERROR: dulwich is still missing after install. Re-run the dependency step:"
        echo "    $VENV_PY -m pip install -r requirements.txt"
        exit 1
    fi
fi
"$VENV_PY" tools/git_sync.py pull

echo "-> Updating dependencies..."
"$VENV_PY" -m pip install -r requirements.txt

echo "-> Upgrading the database..."
echo "   Current version:"
"$VENV_PY" -m flask --app run.py db current || true
echo "   Target version:"
"$VENV_PY" -m flask --app run.py db heads || true
echo "   Large upgrades (e.g. the search-index split) copy data and show"
echo "   progress per step — allow several minutes, do not interrupt."
if ! "$VENV_PY" -m flask --app run.py db upgrade heads; then
    echo "ERROR: database upgrade failed. No automatic re-stamping was attempted."
    echo "Check the error above. An unknown revision may belong to another branch or module."
    echo "Restore the matching migration history; back up the database before recovery."
    exit 1
fi

echo ""
echo "=== Done! Restart the server to use the new version: ==="
echo "    ./run.sh"
