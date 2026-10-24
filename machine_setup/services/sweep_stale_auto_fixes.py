"""Use case: the periodic sweep that keeps a Night auto-fix from sleeping forever.

Nobody may open a page at night, so a killed worker or a lost broker message
must not wait for a page visit to fall back to a person. Every couple of
minutes (``CELERY_BEAT_SCHEDULE``) this finds the help requests of PAUSED runs
whose auto-fix is ``running`` and:

- closes a stale auto-fix turn (``settle_stale_auto_fix_turn``), which settles
  its request as not fixed and notifies the members;
- settles as not fixed a request that has no auto-fix thread at all well past
  ``AUTO_FIX_START_GRACE`` since its auto-fix started: the start task was lost.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.db.models.functions import Coalesce
from django.utils import timezone

from machine_setup.models import SetupConversation
from machine_setup.services.case_auto_fix import settle_stale_auto_fix_turn
from projects.case_help_services import finish_case_auto_fix
from projects.models import CaseHelpRequest, HelpAutoFixState, Project, TestRunStatus

logger = logging.getLogger(__name__)

# How long a running auto-fix may wait for its start task to open its thread.
# The task only creates rows; a queue busy for longer than this falls back to
# a person (the late start task then finds the request settled and does nothing).
AUTO_FIX_START_GRACE = timedelta(minutes=15)

LOST_START_SUMMARY = (
    "Auto-fix could not start: the setup agent was never started for this case."
)


def sweep_stale_auto_fixes() -> None:
    """Settle the running auto-fixes whose turn or start task is gone; never raises.

    One query finds the candidates (usually none); each project with a turn is
    checked with ``settle_stale_auto_fix_turn``, and each lost start is
    re-checked under the project lock before it is settled.
    """
    try:
        candidates = list(
            CaseHelpRequest.objects.filter(
                auto_fix_state=HelpAutoFixState.RUNNING,
                test_run_test_case__test_run__status=TestRunStatus.PAUSED,
            )
            .annotate(
                has_thread=Exists(
                    SetupConversation.objects.filter(help_request_id=OuterRef("pk"))
                ),
                started=Coalesce("auto_fix_started_at", "requested_at"),
            )
            .values_list(
                "pk",
                "test_run_test_case__test_run__project_id",
                "has_thread",
                "started",
            )
        )
    except Exception:
        logger.exception("Sweeping stale auto-fixes failed")
        return
    cutoff = timezone.now() - AUTO_FIX_START_GRACE
    for project_id in {row[1] for row in candidates if row[2]}:
        settle_stale_auto_fix_turn(project_id)
    for help_request_id, project_id, has_thread, started in candidates:
        if not has_thread and started < cutoff:
            _settle_lost_start(help_request_id, project_id)


def _settle_lost_start(help_request_id: int, project_id: int) -> None:
    try:
        with transaction.atomic():
            # The start task opens the thread under this lock, so the check
            # below cannot race it.
            Project.objects.select_for_update().only("id").get(pk=project_id)
            still_lost = (
                CaseHelpRequest.objects.filter(
                    pk=help_request_id, auto_fix_state=HelpAutoFixState.RUNNING
                ).exists()
                and not SetupConversation.objects.filter(
                    help_request_id=help_request_id
                ).exists()
            )
            if not still_lost:
                return
            logger.warning(
                "Auto-fix of help request %s never started; a person takes over",
                help_request_id,
            )
            finish_case_auto_fix(
                help_request_id=help_request_id,
                fixed=False,
                summary=LOST_START_SUMMARY,
            )
    except Exception:
        logger.exception(
            "Settling the lost auto-fix start of help request %s failed",
            help_request_id,
        )
