"""Celery task names other apps send by name.

A leaf module (no imports): ``projects`` enqueues these with
``celery_app.send_task`` so it never imports ``machine_readiness.tasks``
(which imports the agent runtime). Routes are in ``CELERY_TASK_ROUTES``.
"""

from __future__ import annotations

ANALYSE_PRECONDITIONS = "machine_readiness.tasks.analyse_preconditions_task"
SCAN_ARTIFACTS = "machine_readiness.tasks.scan_artifacts_task"
QUICK_CHECK = "machine_readiness.tasks.quick_check_task"
