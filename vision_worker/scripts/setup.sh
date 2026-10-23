#!/usr/bin/env bash
# Set up a vision worker in vision_worker/.venv (Linux or macOS).
# Usage: vision_worker/scripts/setup.sh [--python <python-binary>]
set -euo pipefail

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

if ! command -v "$PYTHON_BIN" &> /dev/null; then
    echo "Error: Python binary '$PYTHON_BIN' not found." >&2
    exit 1
fi

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
VENV_PY="$PROJECT_DIR/.venv/bin/python"
VENV_PIP="$PROJECT_DIR/.venv/bin/pip"

echo "=== Vision Worker Setup ==="
echo ""

if [ ! -d "$PROJECT_DIR/.venv" ]; then
    echo "[1/6] Creating Python virtual environment..."
    "$PYTHON_BIN" -m venv "$PROJECT_DIR/.venv"
else
    echo "[1/6] Virtual environment already exists, skipping..."
fi
"$VENV_PIP" install --quiet --upgrade pip

# torch first, from the index that matches the machine. The CUDA 12.8 wheels
# bundle the CUDA runtime, so only the NVIDIA driver is needed on the host.
echo "[2/6] Installing torch..."
if [[ "$(uname -s)" == "Darwin" ]]; then
    echo "  macOS detected, installing the standard build (includes MPS support)."
    "$VENV_PIP" install --quiet "torch~=2.10.0" "torchvision~=0.25.0"
elif command -v nvidia-smi &> /dev/null; then
    echo "  NVIDIA GPU detected, installing the CUDA 12.8 build."
    "$VENV_PIP" install --quiet --index-url https://download.pytorch.org/whl/cu128 "torch~=2.10.0" "torchvision~=0.25.0"
else
    echo "  WARNING: no NVIDIA GPU detected (nvidia-smi not found); installing the CPU-only build." >&2
    echo "  A CPU worker works but parses each screenshot far more slowly." >&2
    "$VENV_PIP" install --quiet --index-url https://download.pytorch.org/whl/cpu "torch~=2.10.0" "torchvision~=0.25.0"
fi

echo "[3/6] Installing remaining dependencies..."
"$VENV_PIP" install --quiet -r "$PROJECT_DIR/requirements.txt"

echo "[4/6] Downloading OmniParser model weights (this may take a while, ~1.5GB)..."
WEIGHTS_DIR="$PROJECT_DIR/omniparser/weights"
# The download recreates icon_caption/ next to the renamed folder, so a re-run
# skips it once both model folders are in place.
if [ -d "$WEIGHTS_DIR/icon_detect" ] && [ -d "$WEIGHTS_DIR/icon_caption_florence" ]; then
    echo "  Weights already present, skipping..."
else
    "$PROJECT_DIR/.venv/bin/hf" download microsoft/OmniParser-v2.0 --local-dir "$WEIGHTS_DIR"
    if [ -d "$WEIGHTS_DIR/icon_caption" ]; then
        rm -rf "$WEIGHTS_DIR/icon_caption_florence"
        mv "$WEIGHTS_DIR/icon_caption" "$WEIGHTS_DIR/icon_caption_florence"
        echo "  Renamed icon_caption -> icon_caption_florence"
    fi
fi

# EasyOCR/PaddleOCR download their own models when util.utils is imported,
# and the Florence-2 processor comes from the Hugging Face Hub on first load.
# Fetch them now so the first model load does not stall on downloads.
echo "[5/6] Pre-warming OCR engines and the caption processor..."
"$VENV_PY" -c "
import sys
sys.path.insert(0, '$PROJECT_DIR/omniparser')
import util.utils
from transformers import AutoProcessor
AutoProcessor.from_pretrained('microsoft/Florence-2-base', trust_remote_code=True)
"

if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "[6/6] Creating .env from example.env..."
    cp "$PROJECT_DIR/example.env" "$PROJECT_DIR/.env"
    echo ""
    echo "IMPORTANT: edit vision_worker/.env and set VISION_SERVER_URL and VISION_WORKER_TOKEN"
else
    echo "[6/6] .env already exists, skipping..."
fi

echo ""
echo "Setup complete!"
echo ""
echo "To check the install (dependencies, weights, device, model load, one inference):"
echo "  cd $ROOT_DIR"
echo "  vision_worker/.venv/bin/python -m vision_worker.diagnose --check-server"
echo ""
echo "To start the vision worker:"
echo "  vision_worker/.venv/bin/python -m vision_worker.main"
