#!/usr/bin/env bash
set -euo pipefail

if [[ "$EUID" -ne 0 ]]; then
    echo "Error: This script must be run as root. Use: sudo $0" >&2
    exit 1
fi

# Parse --python argument
PYTHON_BIN="python3"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --python)
            PYTHON_BIN="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            echo "Usage: $0 [--python <python-binary>]" >&2
            exit 1
            ;;
    esac
done

# Verify the Python binary exists
if ! command -v "$PYTHON_BIN" &> /dev/null; then
    echo "Error: Python binary '$PYTHON_BIN' not found." >&2
    exit 1
fi

# Check Python version >= 3.13
PYTHON_VERSION=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_MAJOR=$("$PYTHON_BIN" -c "import sys; print(sys.version_info.major)")
PYTHON_MINOR=$("$PYTHON_BIN" -c "import sys; print(sys.version_info.minor)")

if [[ "$PYTHON_MAJOR" -lt 3 ]] || { [[ "$PYTHON_MAJOR" -eq 3 ]] && [[ "$PYTHON_MINOR" -lt 13 ]]; }; then
    echo "Error: Python 3.13+ is required, but '$PYTHON_BIN' is Python $PYTHON_VERSION." >&2
    echo "Hint: specify the correct binary with --python, e.g.: $0 --python python3.13" >&2
    exit 1
fi

echo "Using Python $PYTHON_VERSION ($PYTHON_BIN)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ROOT_DIR="$(dirname "$PROJECT_DIR")"

echo "=== Controller Client Setup ==="
echo ""

# Install system dependencies (Linux only)
if [[ "$(uname -s)" == "Linux" ]]; then
    echo "[1/5] Installing system dependencies..."
    if command -v apt-get &> /dev/null; then
        if ! command -v gnome-screenshot &> /dev/null; then
            echo "  Installing gnome-screenshot (required by PyAutoGUI)..."
            apt-get install -y gnome-screenshot
        else
            echo "  gnome-screenshot already installed."
        fi
    else
        echo "  WARNING: Non-apt system detected. Please install gnome-screenshot manually."
    fi
else
    echo "[1/5] System dependencies check skipped (not Linux)."
fi

# Create virtual environment
if [ ! -d "$PROJECT_DIR/.venv" ]; then
    echo "[2/5] Creating Python virtual environment..."
    "$PYTHON_BIN" -m venv "$PROJECT_DIR/.venv"
else
    echo "[2/5] Virtual environment already exists, skipping..."
fi

# Install dependencies
echo "[3/5] Installing dependencies..."
"$PROJECT_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$PROJECT_DIR/.venv/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"

# Install Playwright browsers
echo "[4/5] Installing Playwright browsers..."
"$PROJECT_DIR/.venv/bin/playwright" install --with-deps

# Copy example.env to .env if not exists
if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "[5/5] Creating .env from example.env..."
    cp "$PROJECT_DIR/example.env" "$PROJECT_DIR/.env"
    echo ""
    echo "IMPORTANT: Edit .env and set your CONTROLLER_API_KEY"
else
    echo "[5/5] .env already exists, skipping..."
fi

echo ""
echo "Setup complete!"
echo ""
echo "To start the controller client:"
echo "  cd $ROOT_DIR"
echo "  controller_client/.venv/bin/python -m controller_client.main"
