#!/usr/bin/env bash
# DocIndex installer (Linux/macOS/Git Bash).
# Creates the virtualenv, installs dependencies, prepares .env,
# creates the database and the first admin user.
set -e
cd "$(dirname "$0")"

echo "=== DocIndex install ==="

# 1. Python check
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found. Install Python 3.11+ first."
    exit 1
fi

# 2. Virtual environment
if [ ! -d venv ]; then
    echo "-> Creating virtual environment..."
    python3 -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate

# 3. Dependencies
echo "-> Installing dependencies..."
pip install -r requirements.txt

# 4. .env with a generated SECRET_KEY (kept if one already exists)
if [ ! -f .env ]; then
    echo "-> Creating .env with a random SECRET_KEY..."
    python -c "import secrets, pathlib; \
        pathlib.Path('.env').write_text( \
        pathlib.Path('.env.example').read_text().replace( \
        'change-me-to-a-long-random-string', secrets.token_hex(32)))"
else
    echo "-> .env already exists, keeping it."
fi

# 5. Database
echo "-> Creating/upgrading the database..."
python -m flask --app run.py db upgrade

# 6. First admin user (prompts; or pass args: ./install.sh user email password)
echo "-> Creating the admin user..."
python create_admin.py "$@"

echo ""
echo "=== Done! To start the app: ==="
echo "    source venv/bin/activate"
echo "    python run.py"
echo "Then open http://localhost:5000"
