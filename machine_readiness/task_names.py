"""Celery task names other apps send by name.

A leaf module (no imports): ``projects`` enqueues these with
``celery_app.send_task`` so it never imports ``machine_readiness.tasks``
(which imports the agent runtime). Routes are in ``CELERY_TASK_ROUTES``.
The scan and the quick check need runner calls, which ``machine_readiness``
gets injected (``runner_ports``); their tasks live in ``projects.tasks``,
the composition root.
"""

from __future__ import annotations

ANALYSE_PRECONDITIONS = "machine_readiness.tasks.analyse_preconditions_task"
SCAN_ARTIFACTS = "projects.tasks.scan_artifacts_task"
QUICK_CHECK = "projects.tasks.quick_check_task"
