"""Use case: a writable member sets the applications that may be captured.

``Project.capture_allowed_processes`` holds normalized process names
(``controller_client.protocol.normalize_process_name``), at most
``CAPTURE_ALLOWED_PROCESSES_MAX``. An empty list turns screenshot capture off.
This module is the only writer of that field.
"""

from __future__ import annotations

from collections.abc import Iterable

from controller_client.protocol import (
    CAPTURE_ALLOWED_PROCESSES_MAX,
    normalize_process_name,
)
from projects.exceptions import CaptureAppsError
from projects.models import Project


def set_capture_allowed_processes(
    *, project: Project, names: Iterable[str]
) -> list[str]:
    """Store the normalized, de-duplicated names (order kept) and return them.

    Blank entries are skipped. Raises ``CaptureAppsError`` (the Settings form checks the same rules first, so
    this only guards the invariant) for a name that
    is not a valid process name or when more than the cap remain.
    """
    allowed: list[str] = []
    for raw in names:
        if not raw.strip():
            continue
        name = normalize_process_name(raw)
        if name is None:
            raise CaptureAppsError(f"Not a valid application name: {raw.strip()[:80]}")
        if name not in allowed:
            allowed.append(name)
    if len(allowed) > CAPTURE_ALLOWED_PROCESSES_MAX:
        raise CaptureAppsError(
            f"List at most {CAPTURE_ALLOWED_PROCESSES_MAX} applications."
        )
    if allowed != project.capture_allowed_processes:
        project.capture_allowed_processes = allowed
        project.save(update_fields=["capture_allowed_processes"])
    return allowed
