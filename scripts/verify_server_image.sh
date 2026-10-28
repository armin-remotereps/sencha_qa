#!/usr/bin/env bash
# Builds the production server image and verifies, inside it, what the server
# needs from the standalone client packages: the shared protocol imports used
# by projects.controller_protocol (controller_client.protocol) and by
# visual_engine (vision_worker.protocol), and the full controller_client source
# tree that generate_controller_client_zip() packages for download. Also
# asserts the per-machine runtime artifacts (venvs, model weights, .env) were
# NOT copied in, and that the old controller-side OmniParser copy is gone.
# Finally builds the Test Runner self-update bundle (projects.runner_bundle)
# the way the server does, with no .env in the image (dummy settings env vars
# only, no database or Redis needed), and checks what the runner will accept:
# version, sha256, size limit, entry paths, and no .env/.venv/logs inside.
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
    controller_client/logs \
    vision_worker/.venv \
    vision_worker/venv \
    vision_worker/omniparser/weights \
    vision_worker/.env; do
    if [ -e "$p" ]; then echo "UNEXPECTED in image: $p"; exit 1; fi
done
if ls -d test-runner-* start-test-runner.* >/dev/null 2>&1; then
    echo "UNEXPECTED in image: Test Runner self-update leftovers (test-runner-*, start-test-runner.*)"; exit 1
fi
echo "client sources present, runtime artifacts and controller OmniParser excluded"
if [ -e .env ]; then echo "UNEXPECTED in image: .env"; exit 1; fi
SECRET_KEY=verify-image \
FIELD_ENCRYPTION_KEY="$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")" \
DB_NAME=verify DB_USER=verify DB_PASSWORD=verify DB_HOST=localhost \
REDIS_URL=redis://localhost:6379/0 \
DJANGO_SETTINGS_MODULE=auto_tester.settings \
python - <<"PY"
import base64
import hashlib
import io
import zipfile

import django

django.setup()

from controller_client.protocol import (
    CLIENT_VERSION,
    MAX_UPDATE_ARCHIVE_BYTES,
    parse_update_runner_payload,
)
from projects.runner_bundle import update_bundle

bundle = update_bundle()
assert bundle.version == CLIENT_VERSION, (bundle.version, CLIENT_VERSION)
assert 0 < len(bundle.archive) <= MAX_UPDATE_ARCHIVE_BYTES, len(bundle.archive)
assert hashlib.sha256(bundle.archive).hexdigest() == bundle.sha256.lower(), "sha256 mismatch"
parse_update_runner_payload(
    {
        "update_id": "00000000-0000-4000-8000-000000000000",
        "target_version": bundle.version,
        "archive_base64": base64.b64encode(bundle.archive).decode("ascii"),
        "archive_sha256": bundle.sha256,
        "archive_size": len(bundle.archive),
    }
)
with zipfile.ZipFile(io.BytesIO(bundle.archive)) as zf:
    names = zf.namelist()
for required in ("controller_client/protocol.py", "controller_client/main.py", "controller_client/requirements.txt"):
    assert required in names, f"bundle is missing {required}"
for name in names:
    parts = name.split("/")
    assert parts[0] == "controller_client" and ".." not in parts and "\\" not in name, f"bad entry {name}"
    assert not ({".env", ".venv", "venv", "logs", "__pycache__"} & set(parts)), f"runtime artifact in bundle: {name}"
    assert not any(p.startswith("test-runner-") for p in parts), f"self-update leftover in bundle: {name}"
print(f"runner update bundle ok: {bundle.version}, {len(bundle.archive)} bytes, {len(names)} entries, no .env")
PY
'
echo "Server image verification passed."
