"""Use case: the periodic sweep that keeps a Night auto-fix from sleeping forever.

Nobody may open a page at night, so a killed worker, a lost broker message or
a settle that failed must not wait for a page visit to fall back to a person.
Every couple of minutes (``CELERY_BEAT_SCHEDULE``) one query finds the help
requests of PAUSED runs whose auto-fix is ``running`` or still has a live
auto-fix turn, and:

- closes a stale live auto-fix turn (``settle_stale_auto_fix_turn``), also when
  its request is no longer ``running``, which settles the request if needed;
- settles a ``running`` request whose thread's turn has ended (at least
  ``AUTO_FIX_SETTLE_GRACE`` ago) but was never settled, from that turn, so a
  reported fix still counts;
- settles as not fixed a ``running`` request that has no auto-fix thread at
  all well past ``AUTO_FIX_START_GRACE``: the start task was lost.

Every settle re-checks the request under the project lock, the lock every
settle path takes, so a request is settled (and its members notified) once.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from machine_setup.models import LIVE_TURN_STATUSES, SetupConversation, SetupTurn
from machine_setup.services.auto_fix_settlement import settle_case_auto_fix
from machine_setup.services.case_auto_fix import settle_stale_auto_fix_turn
from machine_setup.turn_state import auto_fix_turn_live
from projects.case_help_services import finish_case_auto_fix
from projects.models import CaseHelpRequest, HelpAutoFixState, Project, TestRunStatus

logger = logging.getLogger(__name__)

# How long a running auto-fix may wait for its start task to open its thread.
# The task only creates rows; a queue busy for longer than this falls back to
# a person (the late start task then finds the request settled and does nothing).
AUTO_FIX_START_GRACE = timedelta(minutes=15)

# How long after an auto-fix turn ended its own settle may still be running.
AUTO_FIX_SETTLE_GRACE = timedelta(minutes=2)

UNSETTLED_SUMMARY = "The setup agent's turn ended without reporting a result."
LOST_START_SUMMARY = (
    "Auto-fix could not start: the setup agent was never started for this case."
)


def sweep_stale_auto_fixes() -> None:
    """Settle the auto-fixes whose turn, settle or start task is gone; never raises."""
    try:
        live_turn = SetupTurn.objects.filter(
            auto_fix=True,
            conversation__help_request_id=OuterRef("pk"),
            status__in=LIVE_TURN_STATUSES,
        )
        candidates = list(
            CaseHelpRequest.objects.filter(
                test_run_test_case__test_run__status=TestRunStatus.PAUSED,
            )
            .annotate(
                has_live_turn=Exists(live_turn),
                has_thread=Exists(
                    SetupConversation.objects.filter(help_request_id=OuterRef("pk"))
                ),
                started=Coalesce("auto_fix_started_at", "requested_at"),
            )
            .filter(Q(auto_fix_state=HelpAutoFixState.RUNNING) | Q(has_live_turn=True))
            .values_list(
                "pk",
                "test_run_test_case__test_run__project_id",
                "auto_fix_state",
                "has_live_turn",
                "has_thread",
                "started",
            )
        )
    except Exception:
        logger.exception("Sweeping stale auto-fixes failed")
        return
    # A project has at most one live turn; close it if its worker is gone.
    for project_id in {row[1] for row in candidates if row[3]}:
        settle_stale_auto_fix_turn(project_id)
    start_cutoff = timezone.now() - AUTO_FIX_START_GRACE
    for help_request_id, project_id, state, _, has_thread, started in candidates:
        if state != HelpAutoFixState.RUNNING:
            continue
        if has_thread:
            _settle_ended_thread(help_request_id, project_id)
        elif started < start_cutoff:
            _settle_lost_start(help_request_id, project_id)


def _lock_running(help_request_id: int, project_id: int) -> bool:
    """Lock the project; whether the request's auto-fix is still ``running``."""
    Project.objects.select_for_update().only("id").get(pk=project_id)
    return CaseHelpRequest.objects.filter(
        pk=help_request_id, auto_fix_state=HelpAutoFixState.RUNNING
    ).exists()


def _settle_ended_thread(help_request_id: int, project_id: int) -> None:
    """Settle a running request whose auto-fix turn ended but was never settled.

    The turn's end was written, then its settle failed (or the worker died
    between the two). Left alone while a turn is live (stale ones were closed
    above) or the last one ended within ``AUTO_FIX_SETTLE_GRACE``, so a settle
    still in progress is not raced.
    """
    try:
        with transaction.atomic():
            if not _lock_running(help_request_id, project_id):
                return
            if auto_fix_turn_live(help_request_id):
                return
            turn = (
                SetupTurn.objects.filter(
                    auto_fix=True, conversation__help_request_id=help_request_id
                )
                .order_by("-id")
                .first()
            )
            if turn is not None and turn.status in LIVE_TURN_STATUSES:
                # Stale but not closed (its close failed); retried next sweep.
                return
            settle_cutoff = timezone.now() - AUTO_FIX_SETTLE_GRACE
            if turn is not None and (turn.finished_at or turn.created_at) > (
                settle_cutoff
            ):
                return
            logger.warning(
                "Auto-fix of help request %s ended unsettled; settling it now",
                help_request_id,
            )
            if turn is None:
                finish_case_auto_fix(
                    help_request_id=help_request_id,
                    fixed=False,
                    summary=UNSETTLED_SUMMARY,
                )
            else:
                # Honours the thread's report_case_fix, as the turn's own
                # settle would have.
                settle_case_auto_fix(turn)
    except Exception:
        logger.exception(
            "Settling the ended auto-fix of help request %s failed", help_request_id
        )


def _settle_lost_start(help_request_id: int, project_id: int) -> None:
    try:
        with transaction.atomic():
            # The start task opens the thread under this lock, so the check
            # below cannot race it.
            still_lost = (
                _lock_running(help_request_id, project_id)
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
