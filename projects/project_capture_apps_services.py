"""Use case: a writable member sets the applications that may be captured.

``Project.capture_allowed_processes`` holds normalized process names
(``controller_client.protocol.normalize_process_name``), at most
``CAPTURE_ALLOWED_PROCESSES_MAX``. An empty list turns screenshot capture off.
This module is the only writer of that field.
"""

from __future__ import annotations

from collections.abc import Iterable

from django.db import transaction

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


def stored_capture_apps(project: Project) -> list[str]:
    """The project's allowlist as stored: the non-empty strings of
    ``capture_allowed_processes`` ([] when the field is not a list)."""
    raw = project.capture_allowed_processes
    if not isinstance(raw, list):
        return []
    return [name for name in raw if isinstance(name, str) and name]


def add_capture_allowed_processes(*, project: Project, app_ids: Iterable[str]) -> int:
    """Merge ``app_ids`` (the phone picker's ticked ids) into the allowlist and
    return how many were new.

    Under a row lock, so a concurrent edit is not lost: new ids are appended
    after the existing ones in the given order, duplicates skipped. Raises
    ``CaptureAppsError`` for an id that is not a normalized app id, or when the
    result would exceed ``CAPTURE_ALLOWED_PROCESSES_MAX``; nothing is written
    then. ``project`` gets the stored list.
    """
    new_ids: list[str] = []
    for raw in app_ids:
        app_id = normalize_process_name(raw)
        if app_id is None or app_id != raw:
            raise CaptureAppsError(f"Not a valid app id: {raw.strip()[:80]}")
        if app_id not in new_ids:
            new_ids.append(app_id)
    with transaction.atomic():
        locked = (
            Project.objects.select_for_update()
            .only("capture_allowed_processes")
            .get(pk=project.pk)
        )
        current = locked.capture_allowed_processes
        allowed = stored_capture_apps(locked)
        merged = allowed + [app_id for app_id in new_ids if app_id not in allowed]
        if len(merged) > CAPTURE_ALLOWED_PROCESSES_MAX:
            raise CaptureAppsError(
                f"List at most {CAPTURE_ALLOWED_PROCESSES_MAX} applications. "
                f"{max(CAPTURE_ALLOWED_PROCESSES_MAX - len(allowed), 0)} more fit."
            )
        if merged != current:
            locked.capture_allowed_processes = merged
            locked.save(update_fields=["capture_allowed_processes"])
    project.capture_allowed_processes = merged
    return len(merged) - len(allowed)
