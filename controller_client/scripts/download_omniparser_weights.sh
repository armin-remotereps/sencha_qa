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

# The repo ships the caption model as icon_caption, the loader reads
# icon_caption_florence. omniparser_weights.py owns that rename (and repairs a
# folder left behind by an interrupted download) for every setup script, then
# fails loudly if anything is still missing.
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="python3"
fi
"$PYTHON_BIN" "$PROJECT_DIR/omniparser_weights.py" "$WEIGHTS_DIR"

echo "Done. Weights saved to $WEIGHTS_DIR"
