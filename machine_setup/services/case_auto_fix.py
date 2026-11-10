"""Use case: the Night-mode auto-fix of a case that needs help.

``start_case_auto_fix`` opens a new auto-fix thread with the case's problem as
its first message and queues the existing setup turn for it (same machinery
and limits as a person's chat). ``settle_case_auto_fix`` hands the turn's end
back to the paused run. ``settle_stale_auto_fix_turn`` closes a turn whose
worker was killed, so the case falls back to a person (the Machine setup pages
and ``sweep_stale_auto_fixes`` call it).
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from machine_setup.models import (
    LIVE_TURN_STATUSES,
    SetupConversation,
    SetupConversationKind,
    SetupMessage,
    SetupTurn,
)
from machine_setup.services.auto_fix_settlement import (
    settle_case_auto_fix as settle_case_auto_fix,
)
from machine_setup.services.case_help_context import (
    build_help_brief,
    load_help_request,
)
from machine_setup.services.live_turn import resolve_live_turn
from machine_setup.services.queue_turn import broadcast_and_enqueue, create_turn
from machine_setup.services.send_gates import runner_refusal
from machine_setup.services.thread_runner import lock_runner, runner_usable_by_project
from machine_setup.turn_state import is_turn_stale
from projects.case_help_services import finish_case_auto_fix
from projects.models import (
    BlockedReason,
    CaseHelpRequest,
    HelpAutoFixState,
    HelpResolution,
    Project,
    TestRun,
    TestRunStatus,
)
from projects.runner_use import runner_id_of_run
from runners.machine_state import (
    MachineState,
    engine_machine_state,
    runner_machine_state,
)
from runners.models import TestRunner
from visual_engine.services.pool_status import get_visual_engine_status

logger = logging.getLogger(__name__)

_START_REFUSED_PREFIX = "Auto-fix could not start: "
_TURN_LIVE_REASON = (
    "the setup agent was already working on this machine (someone was using "
    "Machine setup)."
)
_MACHINE_NOT_READY_REASON = (
    "the test machine was not ready (the Test Runner must be connected and the "
    "visual engine ready)."
)
_RUNNER_BUSY_REASON = "the Test Runner was busy with something else."
_NO_RUNNER_REASON = "the run has no Test Runner."
_RUNNER_UNAVAILABLE_REASON = (
    "the run's Test Runner is no longer available to this project."
)
_RUNNER_CHANGED_REASON = "the run's Test Runner changed meanwhile."
_INTERNAL_ERROR_REASON = "an internal error occurred."


def start_case_auto_fix(help_request_id: int) -> None:
    """Open the auto-fix thread of a help request and queue its turn.

    Does nothing when the request is resolved, its auto-fix is not pending,
    its run is not paused, or a thread already exists for it (a redelivered
    task). When a turn is live or the machine is not ready, the request falls
    back to a person through ``finish_case_auto_fix(fixed=False)``.
    """
    try:
        refusal = _start_locked(help_request_id)
    except Exception:
        logger.exception("Auto-fix of help request %s failed to start", help_request_id)
        refusal = _INTERNAL_ERROR_REASON
    if refusal is None:
        return
    logger.info("Auto-fix of help request %s not started: %s", help_request_id, refusal)
    finish_case_auto_fix(
        help_request_id=help_request_id,
        fixed=False,
        summary=_START_REFUSED_PREFIX + refusal,
    )


@transaction.atomic
def _start_locked(help_request_id: int) -> str | None:
    """``None`` when started or nothing to do; else why it cannot start."""
    # One query for the brief: request -> case, run.
    request = load_help_request(help_request_id)
    if request is None:
        return None
    pivot = request.test_run_test_case
    run = pivot.test_run
    # Lock order of every runner holder and run write: the run's Test Runner
    # row, then the project row, then the run row.
    runner = lock_runner(runner_id_of_run(run))
    project = Project.objects.select_for_update().only("id").get(pk=run.project_id)
    locked_run = (
        TestRun.objects.select_for_update()
        .filter(pk=run.pk)
        .values_list("status", "runner_id")
        .first()
    )
    pending = CaseHelpRequest.objects.filter(
        pk=help_request_id,
        resolution=HelpResolution.OPEN,
        auto_fix_state=HelpAutoFixState.RUNNING,
    ).exists()
    if locked_run is None or locked_run[0] != TestRunStatus.PAUSED or not pending:
        return None
    if SetupConversation.objects.filter(help_request_id=help_request_id).exists():
        return None
    if locked_run[1] != run.runner_id:
        # Reassigned between the brief load and the run lock.
        return _RUNNER_CHANGED_REASON

    now = timezone.now()
    if resolve_live_turn(project_id=project.pk, now=now) is not None:
        return _TURN_LIVE_REASON
    if runner is None:
        return _NO_RUNNER_REASON
    if not runner_usable_by_project(runner, project.pk):
        return _RUNNER_UNAVAILABLE_REASON
    if runner_refusal(runner, project.pk, main_chat=False) is not None:
        return _RUNNER_BUSY_REASON
    if not _machine_ready_for_auto_fix(runner, request):
        return _MACHINE_NOT_READY_REASON

    brief = build_help_brief(request, audience="auto_fix")
    conversation = SetupConversation.objects.create(
        project=project,
        kind=SetupConversationKind.AUTO_FIX,
        help_request=request,
        created_by=None,
    )
    turn, message = create_turn(
        conversation=conversation,
        requested_by=None,
        text=brief,
        runner_id=runner.runner_id,
        auto_fix=True,
    )
    logger.info(
        "Auto-fix of help request %s started: conversation %s, turn %s",
        help_request_id,
        conversation.pk,
        turn.pk,
    )
    transaction.on_commit(lambda: _enqueue(turn, message))
    return None


def _machine_ready_for_auto_fix(runner: TestRunner, request: CaseHelpRequest) -> bool:
    """READY, or, for a disconnected phone or tablet, the runner connected and
    the visual engine ready: the auto-fix may reconnect the device through the
    runner computer's shell (``adb reconnect``, booting the emulator)."""
    engine = get_visual_engine_status()
    if runner_machine_state(runner, engine) == MachineState.READY:
        return True
    return (
        request.fault == BlockedReason.DEVICE_DISCONNECTED
        and runner.connected
        and engine_machine_state(engine) == MachineState.READY
    )


def _enqueue(turn: SetupTurn, message: SetupMessage) -> None:
    """Queue the turn; when it cannot be queued, a person takes over.

    ``broadcast_and_enqueue`` isolates its broadcasts, so a page update that
    fails never blocks the enqueue. When the enqueue fails it closes the turn
    as failed; the request is then settled not fixed.
    """
    try:
        enqueued = broadcast_and_enqueue(turn, message)
    except Exception:
        # Closing the unqueued turn failed too; settle all the same. The turn
        # row still looks queued: the periodic sweep closes it once stale
        # (``sweep_stale_auto_fixes`` checks every live auto-fix turn of a
        # paused run, whatever its request's state).
        logger.exception("Auto-fix turn %s: enqueue failed", turn.pk)
        enqueued = False
    if not enqueued:
        settle_case_auto_fix(turn)


def settle_stale_auto_fix_turn(project_id: int) -> None:
    """Close the project's live turn if its worker is gone; never raises.

    A closed auto-fix turn settles its help request as not fixed (through
    ``live_turn``), so the case falls back to a person. One cheap query when
    nothing is stale; the project row is locked only when something is.
    Called by the Machine setup pages and the periodic stale sweep.
    """
    try:
        now = timezone.now()
        live = (
            SetupTurn.objects.filter(
                project_id=project_id, status__in=LIVE_TURN_STATUSES
            )
            .only("id", "status", "started_at", "created_at")
            .order_by("-id")
            .first()
        )
        if live is None or not is_turn_stale(live, now):
            return
        with transaction.atomic():
            Project.objects.select_for_update().only("id").get(pk=project_id)
            resolve_live_turn(project_id=project_id, now=now)
    except Exception:
        logger.exception("Closing a stale setup turn of project %s failed", project_id)
