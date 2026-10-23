from __future__ import annotations

import secrets
from collections.abc import Sequence

from django.conf import settings

from vision_worker.protocol import REQUIRED_WORKER_CAPABILITIES, WORKER_VERSION


def is_valid_worker_token(token: str) -> bool:
    """Compare ``token`` with the shared worker secret in constant time.

    An unset ``VISION_WORKER_TOKEN`` rejects every worker.
    """
    expected: str = settings.VISION_WORKER_TOKEN
    if not expected or not token:
        return False
    return secrets.compare_digest(token.encode(), expected.encode())


def check_worker_compatibility(
    worker_version: str, capabilities: Sequence[str]
) -> str | None:
    """Return why the worker cannot serve this server, or None when it can.

    The worker must speak the same major protocol version and advertise every
    required capability; extra capabilities are ignored.
    """
    worker_major = _major_version(worker_version)
    server_major = _major_version(WORKER_VERSION)
    if worker_major is None or worker_major != server_major:
        return (
            f"Vision worker version {worker_version or 'unknown'} is not compatible "
            f"with this server (expected {server_major}.x)"
        )

    advertised = set(capabilities)
    missing = sorted(
        capability.value
        for capability in REQUIRED_WORKER_CAPABILITIES
        if capability.value not in advertised
    )
    if missing:
        return f"Vision worker is missing required capabilities: {', '.join(missing)}"
    return None


def _major_version(version: str) -> int | None:
    major, _, _ = version.strip().partition(".")
    if not major.isdigit():
        return None
    return int(major)
