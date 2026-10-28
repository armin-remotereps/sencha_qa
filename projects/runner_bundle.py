"""The Test Runner source packaged as a ZIP: the download and the self-update bundle.

The tracked ``controller_client/`` source ships inside the server image, so the
bundle is built from ``BASE_DIR/controller_client``. The download carries a
generated ``.env`` with the project's Connection key; the update bundle carries
no ``.env`` at all (the runner copies its own).
"""

from __future__ import annotations

import base64
import functools
import hashlib
import io
import zipfile
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from django.conf import settings

from controller_client.protocol import CLIENT_VERSION, MAX_UPDATE_ARCHIVE_BYTES

_PACKAGE_DIR_NAME = "controller_client"

_EXCLUDE_DIRS = frozenset(
    {
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        "tests",
        "weights",
        "logs",
    }
)
# Managed install folders a self-update creates; never packaged.
_EXCLUDE_DIR_PATTERN = "test-runner-*"
_EXCLUDE_FILES = frozenset({".env"})


@dataclass(frozen=True)
class RunnerBundle:
    """The update bundle sent over the runner's socket."""

    archive: bytes
    # Standard padded base64 of ``archive`` (the runner checks its length).
    archive_base64: str
    # Lower-case hex sha256 of ``archive``.
    sha256: str
    version: str


def _should_include(relative_path: Path) -> bool:
    for part in relative_path.parts[:-1]:
        if part in _EXCLUDE_DIRS or fnmatch(part, _EXCLUDE_DIR_PATTERN):
            return False
    name = relative_path.name
    if name in _EXCLUDE_DIRS or name in _EXCLUDE_FILES:
        return False
    return True


def build_runner_archive(env_content: str | None) -> bytes:
    """ZIP of the runner source under ``controller_client/``.

    ``env_content`` becomes ``controller_client/.env``; ``None`` leaves the
    ``.env`` out (update bundle). Symlinks are skipped, so nothing outside the
    source tree is packaged.
    """
    controller_dir = Path(settings.BASE_DIR) / _PACKAGE_DIR_NAME
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(controller_dir.rglob("*")):
            if file_path.is_symlink() or not file_path.is_file():
                continue
            relative = file_path.relative_to(controller_dir)
            if not _should_include(relative):
                continue
            zf.write(file_path, f"{_PACKAGE_DIR_NAME}/{relative.as_posix()}")
        if env_content is not None:
            zf.writestr(f"{_PACKAGE_DIR_NAME}/.env", env_content)
    return buffer.getvalue()


@functools.cache
def update_bundle() -> RunnerBundle:
    """The self-update bundle of this server's runner version, built once.

    The source is immutable inside the image; in development restart the
    server after editing the runner. Raises ``ValueError`` when the bundle is
    larger than the protocol allows (the runner would refuse it).
    """
    archive = build_runner_archive(None)
    if len(archive) > MAX_UPDATE_ARCHIVE_BYTES:
        raise ValueError(
            f"Test Runner bundle is {len(archive)} bytes; the protocol allows "
            f"{MAX_UPDATE_ARCHIVE_BYTES}"
        )
    return RunnerBundle(
        archive=archive,
        archive_base64=base64.b64encode(archive).decode("ascii"),
        sha256=hashlib.sha256(archive).hexdigest(),
        version=CLIENT_VERSION,
    )
