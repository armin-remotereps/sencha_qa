"""A paused run's help state, and handing a help request to the Night auto-fix.

A leaf module (models, ``machine_setup.task_names``, ``case_help_notifications``
and the Celery app only) so ``services``, ``run_help_mode_services``, the
consumers and the views share it without an import cycle. The auto-fix start
task is sent by name: importing ``machine_setup.tasks`` here would be a cycle.
"""

from __future__ import annotations

import logging
from enum import StrEnum

from django.utils import timezone

from auto_tester.celery import app as celery_app
from machine_setup.task_names import OPEN_PAUSED_HELP_CHAT, START_CASE_AUTO_FIX
from projects.case_help_notifications import notify_case_needs_help
from projects.models import (
    OPEN_PERSON_STEP_STATUSES,
    CaseHelpRequest,
    CasePersonStep,
    HelpAutoFixState,
    PauseMessageState,
    TestRun,
    TestRunStatus,
    TestRunTestCaseStatus,
)

logger = logging.getLogger(__name__)


class RunHelpState(StrEnum):
    """The run page's view of a sleeping run (``TestRunStatusEvent.help_state``)."""

    NONE = "none"
    AUTO_FIXING = "auto_fixing"
    WAITING_FOR_PERSON = "waiting_for_person"
    # Human in the loop: a step of the run waits for a person to perform it
    # (or the person's report waits for "Continue run" past a gate).
    PERSON_STEP = "person_step"


def get_run_help_state(
    test_run: TestRun,
    *,
    person_step_open: bool | None = None,
    latest_auto_fix_state: str | None = None,
) -> RunHelpState:
    """Whether a PAUSED run's help is with the auto-fix agent or a person.

    ``none`` (no query) unless the run is PAUSED. ``person_step`` while a case
    of the run has an open person step and is not itself in Need help (a fault
    while confirming a report opens a help request: that one wins); one
    ``exists()``. Otherwise ``auto_fixing`` while the auto-fix of the run's
    latest help request is running, even when a person
    already resolved that request (the run continues when that agent stops).
    Older requests are ignored: they belong to earlier pauses. Otherwise
    ``waiting_for_person``: an open request, or a resolved one whose queueing
    waits for the gates ("Continue run"). One more query.

    ``person_step_open``: the caller already knows whether such a person step
    exists (the run page loads it for its banner); skips the ``exists()``.
    ``latest_auto_fix_state``: the caller already loaded the run's latest
    help request (its ``auto_fix_state``, or ``""`` when the run has none);
    skips the second query.
    """
    if test_run.status != TestRunStatus.PAUSED:
        return RunHelpState.NONE
    if person_step_open is None:
        person_step_open = (
            CasePersonStep.objects.filter(
                test_run_test_case__test_run_id=test_run.pk,
                status__in=OPEN_PERSON_STEP_STATUSES,
            )
            .exclude(test_run_test_case__status=TestRunTestCaseStatus.NEED_HELP)
            .exists()
        )
    if person_step_open:
        return RunHelpState.PERSON_STEP
    latest_state = latest_auto_fix_state
    if latest_state is None:
        latest_state = (
            CaseHelpRequest.objects.filter(test_run_test_case__test_run_id=test_run.pk)
            .order_by("-requested_at", "-id")
            .values_list("auto_fix_state", flat=True)
            .first()
        )
    if latest_state == HelpAutoFixState.RUNNING:
        return RunHelpState.AUTO_FIXING
    return RunHelpState.WAITING_FOR_PERSON


def mark_auto_fix_running(request: CaseHelpRequest) -> None:
    """Set the request's auto-fix running from now (not saved).

    ``auto_fix_started_at`` lets the stale sweep tell a start task still in
    the queue from a lost one.
    """
    request.auto_fix_state = HelpAutoFixState.RUNNING
    request.auto_fix_started_at = timezone.now()


def enqueue_case_auto_fix(help_request_id: int) -> None:
    """Hand the request to the auto-fix; on a broker error, fall back to people."""
    try:
        celery_app.send_task(START_CASE_AUTO_FIX, args=[help_request_id])
    except Exception:
        logger.exception("Could not enqueue the auto-fix of help %d", help_request_id)
        CaseHelpRequest.objects.filter(
            pk=help_request_id, auto_fix_state=HelpAutoFixState.RUNNING
        ).update(auto_fix_state=HelpAutoFixState.NOT_FIXED)
        notify_case_needs_help(help_request_id, after_auto_fix=True)


_OPEN_DISCUSSION_YOURSELF = "Open the discussion yourself."


def enqueue_open_paused_help_chat(help_request_id: int) -> None:
    """Open the paused case's discussion and send the pauser's message.

    Sent by name (``machine_setup.tasks`` imports this package). On a broker
    error the message is marked not sent, so the page offers a manual open;
    nothing raises.
    """
    try:
        celery_app.send_task(OPEN_PAUSED_HELP_CHAT, args=[help_request_id])
    except Exception:
        logger.exception(
            "Could not enqueue the discussion of paused help %d", help_request_id
        )
        CaseHelpRequest.objects.filter(
            pk=help_request_id, pause_message_state=PauseMessageState.PENDING
        ).update(
            pause_message_state=PauseMessageState.REFUSED,
            pause_message_note=_OPEN_DISCUSSION_YOURSELF,
        )
