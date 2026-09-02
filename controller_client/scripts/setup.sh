#!/usr/bin/env bash
set -euo pipefail

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

# The server's cleanup_environment action empties the Downloads folder between
# test cases (CONTROLLER_CLEANUP_DIR, default ~/Downloads). The client refuses
# to delete its own files, but installing it there still means every cleanup
# skips part of the folder and logs warnings.
case "$PROJECT_DIR/" in
    "$HOME/Downloads/"*)
        echo "WARNING: the controller client is installed inside $HOME/Downloads." >&2
        echo "         Test-run cleanup empties that folder. Move the client elsewhere (e.g. $HOME/controller_client)" >&2
        echo "         or point CONTROLLER_CLEANUP_DIR in .env at a different folder." >&2
        echo ""
        ;;
esac

# Install system dependencies (Linux only)
if [[ "$(uname -s)" == "Linux" ]]; then
    echo "[1/9] Installing system dependencies..."
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
    echo "[1/9] System dependencies check skipped (not Linux)."
fi

# Create virtual environment
if [ ! -d "$PROJECT_DIR/.venv" ]; then
    echo "[2/9] Creating Python virtual environment..."
    "$PYTHON_BIN" -m venv "$PROJECT_DIR/.venv"
else
    echo "[2/9] Virtual environment already exists, skipping..."
fi

"$PROJECT_DIR/.venv/bin/pip" install --quiet --upgrade pip

# Install torch/torchvision with a platform-appropriate build: CUDA wheel on
# Linux/Windows with an NVIDIA GPU detected, CPU-only wheel otherwise (macOS
# wheels already include MPS support with no separate index needed).
echo "[3/9] Installing torch (platform-appropriate build)..."
if [[ "$(uname -s)" == "Darwin" ]]; then
    echo "  macOS detected, installing standard torch build (includes MPS support)."
    "$PROJECT_DIR/.venv/bin/pip" install --quiet "torch~=2.10.0" "torchvision~=0.25.0"
elif command -v nvidia-smi &> /dev/null; then
    echo "  NVIDIA GPU detected, installing CUDA-enabled torch."
    "$PROJECT_DIR/.venv/bin/pip" install --quiet "torch~=2.10.0" "torchvision~=0.25.0"
else
    echo "  No NVIDIA GPU detected, installing CPU-only torch."
    "$PROJECT_DIR/.venv/bin/pip" install --quiet --index-url https://download.pytorch.org/whl/cpu "torch~=2.10.0" "torchvision~=0.25.0"
fi

# Install remaining dependencies
echo "[4/9] Installing remaining dependencies..."
"$PROJECT_DIR/.venv/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"

# Install Playwright browsers
echo "[5/9] Installing Playwright browsers..."
"$PROJECT_DIR/.venv/bin/playwright" install --with-deps

# Download OmniParser model weights (huggingface_hub[cli] comes from
# requirements.txt above, resolved together with transformers instead of a
# separate late pip install that could drag it past transformers' <1.0 ceiling)
echo "[6/9] Downloading OmniParser model weights (this may take a while, ~1.5GB)..."
"$SCRIPT_DIR/download_omniparser_weights.sh" "$PROJECT_DIR/omniparser/weights"

# Pre-warm the OCR engines' own model downloads (EasyOCR/PaddleOCR construct
# their models at import time, independent of the OmniParser weights above).
# Without this, that download happens silently on the first real
# find_element call, on top of the OmniParser model load, risking a timeout.
echo "[7/9] Pre-warming OCR engines (downloads their own models on first use)..."
"$PROJECT_DIR/.venv/bin/python" -c "
import sys
sys.path.insert(0, '$PROJECT_DIR/omniparser')
import util.utils
"

# Copy example.env to .env if not exists
if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "[8/9] Creating .env from example.env..."
    cp "$PROJECT_DIR/example.env" "$PROJECT_DIR/.env"
    echo ""
    echo "IMPORTANT: Edit .env and set your CONTROLLER_API_KEY"
else
    echo "[8/9] .env already exists, skipping..."
fi

# Verify the environment isn't left in the corrupted-certifi state that has
# broken click/screenshot tooling on client machines (partial overwrite
# across the separate pip install passes above). Self-heal automatically if
# it is, since fixing it here is far cheaper than debugging it mid test run.
echo "[9/9] Verifying environment..."
if ! "$PROJECT_DIR/.venv/bin/python" -c "import certifi; certifi.where()" &> /dev/null; then
    echo "  certifi looks corrupted, reinstalling..."
    "$PROJECT_DIR/.venv/bin/pip" install --quiet --force-reinstall --no-cache-dir certifi requests urllib3
    if ! "$PROJECT_DIR/.venv/bin/python" -c "import certifi; certifi.where()" &> /dev/null; then
        echo "Error: certifi is still broken after a forced reinstall. Try deleting $PROJECT_DIR/.venv and re-running this script." >&2
        exit 1
    fi
fi

echo ""
echo "Setup complete!"
echo ""
echo "To start the controller client:"
echo "  cd $ROOT_DIR"
echo "  controller_client/.venv/bin/python -m controller_client.main"
echo ""
echo "To check the OmniParser setup end to end (dependencies, weights, screenshot, model load, one inference):"
echo "  controller_client/.venv/bin/python -m controller_client.diagnose"
