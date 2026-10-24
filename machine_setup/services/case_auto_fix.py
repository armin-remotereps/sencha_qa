"""Use case: the Night-mode auto-fix of a case that needs help.

``start_case_auto_fix`` opens a new auto-fix thread with the case's problem as
its first message and queues the existing setup turn for it (same machinery
and limits as a person's chat). ``settle_case_auto_fix`` hands the turn's end
back to the paused run. ``settle_stale_auto_fix_turn`` closes a turn whose
worker was killed, so the case falls back to a person.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from agents.checkpoint import checkpoint_from_json
from agents.types import OrchestratorCheckpoint
from machine_setup.auto_fix_brief import (
    ACTIONS_DIGEST_MAX,
    AutoFixBriefData,
    PlanStep,
    build_auto_fix_brief,
)
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
from machine_setup.services.live_turn import resolve_live_turn
from machine_setup.services.queue_turn import broadcast_and_enqueue, create_turn
from machine_setup.turn_state import is_turn_stale
from projects.case_help_services import finish_case_auto_fix
from projects.models import (
    CaseHelpRequest,
    HelpAutoFixState,
    HelpResolution,
    Project,
    TestRun,
    TestRunStatus,
)
from projects.services import MachineState, get_machine_state
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
    request = (
        CaseHelpRequest.objects.select_related(
            "test_run_test_case__test_case", "test_run_test_case__test_run"
        )
        .filter(pk=help_request_id)
        .first()
    )
    if request is None:
        return None
    pivot = request.test_run_test_case
    run = pivot.test_run
    # Lock order of every run write: project row, then run row.
    project = (
        Project.objects.select_for_update()
        .only("id", "agent_connected")
        .get(pk=run.project_id)
    )
    run_status = (
        TestRun.objects.select_for_update()
        .filter(pk=run.pk)
        .values_list("status", flat=True)
        .first()
    )
    pending = CaseHelpRequest.objects.filter(
        pk=help_request_id,
        resolution=HelpResolution.OPEN,
        auto_fix_state=HelpAutoFixState.RUNNING,
    ).exists()
    if run_status != TestRunStatus.PAUSED or not pending:
        return None
    if SetupConversation.objects.filter(help_request_id=help_request_id).exists():
        return None

    if resolve_live_turn(project_id=project.pk, now=timezone.now()) is not None:
        return _TURN_LIVE_REASON
    if get_machine_state(project, get_visual_engine_status()) != MachineState.READY:
        return _MACHINE_NOT_READY_REASON

    brief = build_auto_fix_brief(_brief_data(request))
    conversation = SetupConversation.objects.create(
        project=project,
        kind=SetupConversationKind.AUTO_FIX,
        help_request=request,
        created_by=None,
    )
    turn, message = create_turn(
        conversation=conversation, requested_by=None, text=brief, auto_fix=True
    )
    logger.info(
        "Auto-fix of help request %s started: conversation %s, turn %s",
        help_request_id,
        conversation.pk,
        turn.pk,
    )
    transaction.on_commit(lambda: _enqueue(turn, message))
    return None


def _enqueue(turn: SetupTurn, message: SetupMessage) -> None:
    try:
        enqueued = broadcast_and_enqueue(turn, message)
    except Exception:
        # A broadcast failed; the turn stays queued and, if no worker ever
        # runs it, the stale-turn close settles it later.
        logger.exception("Auto-fix turn %s: broadcast failed", turn.pk)
        return
    if not enqueued:
        # The turn was closed as failed; the person takes over.
        settle_case_auto_fix(turn)


def _brief_data(request: CaseHelpRequest) -> AutoFixBriefData:
    pivot = request.test_run_test_case
    case = pivot.test_case
    checkpoint = checkpoint_from_json(pivot.execution_checkpoint)
    plan = (
        tuple(
            PlanStep(description=task.description, expected_result=task.expected_result)
            for task in checkpoint.plan
        )
        if checkpoint is not None
        else ()
    )
    return AutoFixBriefData(
        run_name=pivot.test_run.name,
        case_title=case.title,
        preconditions=case.preconditions,
        steps=case.steps,
        expected=case.expected,
        plan=plan,
        step_num=request.step_num,
        reason=request.reason,
        ask=request.ask,
        actions_digest=_failed_attempt_digest(checkpoint, request.step_num),
    )


def _failed_attempt_digest(
    checkpoint: OrchestratorCheckpoint | None, step_num: int
) -> str:
    """The actions digest of the step's last failed attempt, if any."""
    if checkpoint is None:
        return ""
    for attempt in reversed(checkpoint.attempts):
        if (
            attempt.step_num == step_num
            and attempt.result.status == "fail"
            and attempt.result.actions_digest
        ):
            return attempt.result.actions_digest[:ACTIONS_DIGEST_MAX]
    return ""


def settle_stale_auto_fix_turn(project: Project) -> None:
    """Close the project's live turn if its worker is gone; never raises.

    A closed auto-fix turn settles its help request as not fixed (through
    ``live_turn``), so the case falls back to a person. One cheap query when
    nothing is stale; the project row is locked only when something is.
    """
    try:
        now = timezone.now()
        live = (
            SetupTurn.objects.filter(project=project, status__in=LIVE_TURN_STATUSES)
            .only("id", "status", "started_at", "created_at")
            .order_by("-id")
            .first()
        )
        if live is None or not is_turn_stale(live, now):
            return
        with transaction.atomic():
            Project.objects.select_for_update().only("id").get(pk=project.pk)
            resolve_live_turn(project_id=project.pk, now=now)
    except Exception:
        logger.exception("Closing a stale setup turn of project %s failed", project.pk)
