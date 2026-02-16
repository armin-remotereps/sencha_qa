#!/usr/bin/env bash
set -euo pipefail

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
            sudo apt-get install -y gnome-screenshot
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
    python3 -m venv "$PROJECT_DIR/.venv"
else
    echo "[2/5] Virtual environment already exists, skipping..."
fi

# Install dependencies
echo "[3/5] Installing dependencies..."
"$PROJECT_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$PROJECT_DIR/.venv/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"

# Install Playwright browsers
echo "[4/5] Installing Playwright browsers..."
"$PROJECT_DIR/.venv/bin/playwright" install

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
