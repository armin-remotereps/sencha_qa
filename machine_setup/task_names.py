"""Celery task names other apps send by name.

A leaf module (no imports): ``projects`` enqueues these with
``celery_app.send_task`` because importing ``machine_setup.tasks`` from
``projects.services`` would be an import cycle.
"""

from __future__ import annotations

START_CASE_AUTO_FIX = "machine_setup.tasks.start_case_auto_fix"

# Scheduled by CELERY_BEAT_SCHEDULE in settings (by this name).
SWEEP_STALE_AUTO_FIXES = "machine_setup.tasks.sweep_stale_auto_fixes"
