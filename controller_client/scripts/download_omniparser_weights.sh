#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEIGHTS_DIR="${1:-$PROJECT_DIR/omniparser/weights}"

# huggingface_hub[cli]'s `hf` entry point is installed into the project venv,
# not onto PATH — this script may run without that venv activated, so it
# must call the venv's own binary rather than a bare `hf`.
HF_BIN="$PROJECT_DIR/.venv/bin/hf"
if [ ! -x "$HF_BIN" ]; then
    HF_BIN="hf"
fi

echo "Downloading OmniParser V2 weights to $WEIGHTS_DIR ..."
"$HF_BIN" download microsoft/OmniParser-v2.0 --local-dir "$WEIGHTS_DIR"

if [ -d "$WEIGHTS_DIR/icon_caption" ] && [ ! -d "$WEIGHTS_DIR/icon_caption_florence" ]; then
    mv "$WEIGHTS_DIR/icon_caption" "$WEIGHTS_DIR/icon_caption_florence"
    echo "Renamed icon_caption -> icon_caption_florence"
fi

echo "Done. Weights saved to $WEIGHTS_DIR"
