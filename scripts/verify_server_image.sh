#!/usr/bin/env bash
# Builds the production server image and verifies, inside it, the two things
# the server needs from controller_client/: the shared protocol import used by
# projects.controller_protocol, and the full source tree that
# generate_controller_client_zip() packages for download. Also asserts the
# per-machine runtime artifacts (venv, model weights) were NOT copied in.
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
python -c "from projects.controller_protocol import ActionTypeRegistry; print(\"protocol import ok\")"
for f in \
    controller_client/__init__.py \
    controller_client/client.py \
    controller_client/protocol.py \
    controller_client/exceptions.py \
    controller_client/omniparser_executor.py \
    controller_client/omniparser/util/utils.py \
    controller_client/omniparser/util/omniparser.py \
    controller_client/scripts/setup.sh \
    controller_client/scripts/setup.ps1 \
    controller_client/scripts/setup.bat \
    controller_client/requirements.txt \
    controller_client/example.env; do
    if [ ! -f "$f" ]; then echo "MISSING: $f"; exit 1; fi
done
for d in controller_client/.venv controller_client/omniparser/weights; do
    if [ -e "$d" ]; then echo "UNEXPECTED in image: $d"; exit 1; fi
done
if [ -e controller_client/.env ]; then echo "UNEXPECTED in image: controller_client/.env"; exit 1; fi
echo "controller source present, runtime artifacts excluded"
'
echo "Server image verification passed."
