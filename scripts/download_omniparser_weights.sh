#!/bin/bash
set -euo pipefail

WEIGHTS_DIR="${1:-./OmniParser/weights}"

echo "Downloading OmniParser V2 weights to $WEIGHTS_DIR ..."
hf download microsoft/OmniParser-v2.0 --local-dir "$WEIGHTS_DIR"

if [ -d "$WEIGHTS_DIR/icon_caption" ] && [ ! -d "$WEIGHTS_DIR/icon_caption_florence" ]; then
    mv "$WEIGHTS_DIR/icon_caption" "$WEIGHTS_DIR/icon_caption_florence"
    echo "Renamed icon_caption -> icon_caption_florence"
fi

echo "Done. Weights saved to $WEIGHTS_DIR"
