#!/usr/bin/env bash
# Builds the production server image and verifies, inside it, what the server
# needs from the standalone client packages: the shared protocol imports used
# by projects.controller_protocol (controller_client.protocol) and by
# visual_engine (vision_worker.protocol), and the full controller_client source
# tree that generate_controller_client_zip() packages for download. Also
# asserts the per-machine runtime artifacts (venvs, model weights, .env) were
# NOT copied in, and that the old controller-side OmniParser copy is gone.
#
# Usage: scripts/verify_server_image.sh [image-tag]
set -euo pipefail

IMAGE="${1:-local/auto_tester:verify}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "Building $IMAGE from $ROOT ..."
docker build -t "$IMAGE" "$ROOT"

echo "Verifying image contents ..."
docker run --rm --entrypoint sh "$IMAGE" -c '
set -e
cd /src
python -c "from projects.controller_protocol import ActionTypeRegistry; print(\"controller protocol import ok\")"
python -c "from vision_worker.protocol import MessageType; print(\"vision worker protocol import ok\")"
for f in \
    controller_client/__init__.py \
    controller_client/client.py \
    controller_client/protocol.py \
    controller_client/exceptions.py \
    controller_client/scripts/setup.sh \
    controller_client/scripts/setup.ps1 \
    controller_client/scripts/setup.bat \
    controller_client/requirements.txt \
    controller_client/example.env \
    vision_worker/__init__.py \
    vision_worker/protocol.py; do
    if [ ! -f "$f" ]; then echo "MISSING: $f"; exit 1; fi
done
for p in \
    controller_client/omniparser \
    controller_client/omniparser_executor.py \
    controller_client/.venv \
    controller_client/.env \
    vision_worker/.venv \
    vision_worker/venv \
    vision_worker/omniparser/weights \
    vision_worker/.env; do
    if [ -e "$p" ]; then echo "UNEXPECTED in image: $p"; exit 1; fi
done
echo "client sources present, runtime artifacts and controller OmniParser excluded"
'
echo "Server image verification passed."
