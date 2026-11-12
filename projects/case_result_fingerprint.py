"""Fingerprint of a finished case's result and the admin edit marker (leaf).

Imports only ``projects.models``, so TestRail planners, manual refinement and
pages can read the marker without importing service modules.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from projects.models import TestRunTestCase


def result_fingerprint(pivot: TestRunTestCase) -> str:
    """sha256 of the case's status, blocked reason, finish time and result."""
    canonical = [
        pivot.status,
        pivot.blocked_reason,
        pivot.finished_at.isoformat() if pivot.finished_at else "",
        pivot.result,
    ]
    return hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def admin_edit_is_current(pivot: TestRunTestCase) -> bool:
    """Whether an admin edit is the case's current result (no query).

    A new verdict (Resume, Re-run, help, Apply, Reset) changes the
    fingerprint, so the marker drops out by itself.
    """
    return bool(pivot.admin_edit_fingerprint) and (
        pivot.admin_edit_fingerprint == result_fingerprint(pivot)
    )


def admin_edit_marker(pivot: TestRunTestCase) -> tuple[str, datetime] | None:
    """``(label, edited_at)`` of the current admin edit, else None."""
    if not admin_edit_is_current(pivot) or pivot.admin_edited_at is None:
        return None
    return pivot.admin_edited_label, pivot.admin_edited_at
