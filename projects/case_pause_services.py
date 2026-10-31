"""A person pauses a case to discuss it with the agent (``docs/plan/feat-pause-and-discuss.md``).

A running case (IN_PROGRESS in a STARTED run) gets a pause request on its row
for the run's current generation; the worker stops at the agent's next action
(``services._build_cancellation_check``) and opens the help request itself. A
case whose person step waits (AWAITING_PERSON in a PAUSED run, step
``waiting``) is converted at once: the step is cancelled, the case needs help
and the discussion opens. Both write the help request through
``case_pause_rows.open_pause_help_request``.

No credit, no test-session activity and no throttle here: a pause decides
nothing about the case (the Test Runner seam throttles its socket surface in
``runner_case_pause_services``).

Lock order: project, run, the case's open person step, then the case.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from agents.checkpoint import checkpoint_from_json, without_person_step
from notifications.services.inbox_broadcast import broadcast_project_attention_changed
from projects.case_help_services import HelpPanel
from projects.case_help_state import enqueue_open_paused_help_chat
from projects.case_pause_policy import (
    PAUSE_ALREADY_NEEDS_HELP,
    PAUSE_LABEL_MAX_LENGTH,
    PAUSE_MARK_RECORDING,
    PAUSE_NOT_RUNNING,
    PAUSE_NOTHING_TO_DISMISS,
    PAUSE_RUN_NOT_ACTIVE,
    PAUSE_STEP_BEING_CONFIRMED,
    CasePauseOutcome,
    pause_continue_mode,
)
from projects.case_pause_rows import (
    PAUSED_CASE_FIELDS,
    PauseRequestSnapshot,
    clear_pause_request,
    mark_case_paused_for_help,
    open_pause_help_request,
    pause_pending_for,
)
from projects.exceptions import CasePauseError
from projects.models import (
    ACTIVE_RUN_STATUSES,
    OPEN_PERSON_STEP_STATUSES,
    PAUSE_MESSAGE_MAX_LENGTH,
    PAUSE_REQUEST_FIELDS,
    CasePersonStep,
    PersonStepClose,
    PersonStepKind,
    PersonStepStatus,
    TestRun,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.person_mark_verdict import MARK_CLOSE_REASONS
from projects.person_step_actor import PersonStepActor
from projects.run_reopen_services import lock_project_and_run
from projects.services import broadcast_run_and_pivots

__all__ = [
    "CasePauseOutcome",
    "CasePausePanel",
    "dismiss_dropped_pause",
    "get_case_pause_panel",
    "get_pause_requester_label",
    "pause_pending_for",
    "request_case_pause",
]

_NOT_CONFIRMED = (
    "The agent could not confirm this step. Answer it on the Your turn card first."
)
_PIVOT_SAVE_FIELDS = [*PAUSE_REQUEST_FIELDS, "updated_at"]


@dataclass(frozen=True)
class CasePausePanel:
    """The pause state of a run case, for its case page, run page and the runner."""

    # A Pause button applies now (running case, or a waiting person step).
    can_pause: bool
    # Why it does not (user-safe); "" when it can or a pause is pending.
    pause_blocker: str
    # A pause was requested for the run's current generation; the worker has
    # not stopped yet.
    pause_pending: bool
    pause_requested_by_label: str
    pause_requested_at_iso: str
    # The case's open help request was opened by a person's pause.
    paused: bool
    # Who paused (or asked to, when the agent's need_help or a fault won);
    # "" when no person is recorded on the open request.
    paused_by_label: str
    # The open request's discussion thread (help chat); None before it opens.
    discussion_thread_id: int | None
    # ``PauseMessageState`` of the open request's first message.
    pause_message_state: str
    # User-safe reason the message was not sent ("" otherwise).
    pause_message_note: str
    # The open request's first message, for a re-send when refused or dropped.
    pause_message: str
    # The message of a pause the verdict beat ("Too late to pause"); "" when none.
    dropped_message: str


def request_case_pause(
    *, pivot: TestRunTestCase, actor: PersonStepActor, message: str = ""
) -> CasePauseOutcome:
    """Ask a running case to pause, or pause a case whose person step waits.

    ``message``: an optional first message for the agent (capped at
    ``PAUSE_MESSAGE_MAX_LENGTH``), sent as the pauser's turn once the
    discussion opens.

    - Running case: the request is stored for the locked run's generation
      (``REQUESTED``). When one already exists for it, the first requester
      and message are kept (``ALREADY_REQUESTED``); a caller that passed a
      message tells the person it was not sent (``PAUSE_SECOND_MESSAGE``).
    - Waiting person step: the step is cancelled (closed ``paused``), the
      checkpoint ``person`` key dropped, the help request opened (current
      screen, or restore for a restore hand-off), the case needs help and
      the run stays paused (``PAUSED``); the discussion opens on commit.

    Raises ``CasePauseError`` (user-safe) when the run is not active, the
    case already needs help, a person's mark is recording its evidence, a
    reported step is being confirmed, or the case neither runs nor waits for
    a person.
    """
    message = message.strip()[:PAUSE_MESSAGE_MAX_LENGTH]
    with transaction.atomic():
        project, test_run = lock_project_and_run(pivot.test_run_id)
        step = (
            CasePersonStep.objects.select_for_update()
            .filter(
                test_run_test_case_id=pivot.pk, status__in=OPEN_PERSON_STEP_STATUSES
            )
            .first()
        )
        locked_pivot = (
            TestRunTestCase.objects.select_for_update()
            .defer("logs")
            .filter(pk=pivot.pk, test_run_id=test_run.pk)
            .first()
        )
        if locked_pivot is None:
            raise CasePauseError(PAUSE_NOT_RUNNING)
        if test_run.status not in ACTIVE_RUN_STATUSES:
            raise CasePauseError(PAUSE_RUN_NOT_ACTIVE)
        if locked_pivot.status == TestRunTestCaseStatus.NEED_HELP:
            raise CasePauseError(PAUSE_ALREADY_NEEDS_HELP)
        if (
            step is not None
            and step.status == PersonStepStatus.REPORTED
            and step.close_reason in MARK_CLOSE_REASONS
        ):
            # A person's Mark failed / blocked: its evidence pass must not stop.
            raise CasePauseError(PAUSE_MARK_RECORDING)
        if (
            test_run.status == TestRunStatus.STARTED
            and locked_pivot.status == TestRunTestCaseStatus.IN_PROGRESS
        ):
            return _request_running_pause(test_run, locked_pivot, actor, message)
        if (
            test_run.status == TestRunStatus.PAUSED
            and locked_pivot.status == TestRunTestCaseStatus.AWAITING_PERSON
            and step is not None
        ):
            if step.status == PersonStepStatus.WAITING:
                return _pause_waiting_person_step(
                    project.pk, test_run, locked_pivot, step, actor, message
                )
            if step.status == PersonStepStatus.NOT_CONFIRMED:
                raise CasePauseError(_NOT_CONFIRMED)
        if step is not None and step.status == PersonStepStatus.REPORTED:
            # Re-queued for the agent's check, not picked up yet.
            raise CasePauseError(PAUSE_STEP_BEING_CONFIRMED)
        raise CasePauseError(PAUSE_NOT_RUNNING)


def get_case_pause_panel(
    test_run: TestRun,
    pivot: TestRunTestCase,
    *,
    help_panel: HelpPanel | None = None,
    person_step_status: str | None = None,
    person_step_close_reason: str | None = None,
) -> CasePausePanel:
    """The case's pause state from loaded rows (no queries).

    ``help_panel``: the run's help panel when the caller built it (its
    request and thread are read only when they are this case's).
    ``person_step_status``: the status of the case's open person step, if any.
    ``person_step_close_reason``: that step's close reason, if any.
    """
    pending = (
        pivot.status == TestRunTestCaseStatus.IN_PROGRESS
        and pivot.pause_requested_generation is not None
        and pivot.pause_requested_generation == test_run.execution_generation
    )
    can_pause, blocker = _can_pause(
        test_run, pivot, pending, person_step_status, person_step_close_reason
    )

    request = None
    thread_id: int | None = None
    if (
        help_panel is not None
        and help_panel.is_open
        and help_panel.request.test_run_test_case_id == pivot.pk
    ):
        request = help_panel.request
        thread = help_panel.thread
        if thread is not None and thread.archived_at is None:
            thread_id = thread.pk
    person_recorded = request is not None and request.has_pending_person

    dropped = (
        pivot.status != TestRunTestCaseStatus.IN_PROGRESS
        and pivot.pause_requested_generation is not None
        and pivot.pause_message != ""
    )
    return CasePausePanel(
        can_pause=can_pause,
        pause_blocker=blocker,
        pause_pending=pending,
        pause_requested_by_label=pivot.pause_requested_label if pending else "",
        pause_requested_at_iso=(
            pivot.pause_requested_at.isoformat()
            if pending and pivot.pause_requested_at is not None
            else ""
        ),
        paused=request is not None and request.is_person_pause,
        paused_by_label=request.paused_by_label if request is not None else "",
        discussion_thread_id=thread_id,
        pause_message_state=(
            request.pause_message_state
            if request is not None and person_recorded
            else ""
        ),
        pause_message_note=(
            request.pause_message_note
            if request is not None and person_recorded
            else ""
        ),
        pause_message=(
            request.pause_message if request is not None and person_recorded else ""
        ),
        dropped_message=pivot.pause_message if dropped else "",
    )


def get_pause_requester_label(pivot: TestRunTestCase) -> str:
    """Who asked to pause the case, as stored now ("" when nobody)."""
    return (
        TestRunTestCase.objects.filter(pk=pivot.pk)
        .values_list("pause_requested_label", flat=True)
        .first()
        or ""
    )


def dismiss_dropped_pause(*, pivot: TestRunTestCase) -> None:
    """Forget the "Too late to pause" record of a case (Dismiss on the notice).

    The view authorizes the writable member. Raises ``CasePauseError(PAUSE_NOTHING_TO_DISMISS)`` while the case runs
    (its request is live) or when there is no record.
    """
    with transaction.atomic():
        locked_pivot = (
            TestRunTestCase.objects.select_for_update()
            .defer("logs")
            .filter(pk=pivot.pk, test_run_id=pivot.test_run_id)
            .first()
        )
        if (
            locked_pivot is None
            or locked_pivot.status == TestRunTestCaseStatus.IN_PROGRESS
            or locked_pivot.pause_requested_generation is None
        ):
            raise CasePauseError(PAUSE_NOTHING_TO_DISMISS)
        clear_pause_request(locked_pivot)
        locked_pivot.save(update_fields=_PIVOT_SAVE_FIELDS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request_running_pause(
    test_run: TestRun,
    pivot: TestRunTestCase,
    actor: PersonStepActor,
    message: str,
) -> CasePauseOutcome:
    """Store the request for the locked generation (run and case locked)."""
    generation = test_run.execution_generation
    if pivot.pause_requested_generation == generation:
        return CasePauseOutcome.ALREADY_REQUESTED
    pivot.pause_requested_generation = generation
    pivot.pause_requested_by = actor.user
    pivot.pause_requested_label = actor.label()[:PAUSE_LABEL_MAX_LENGTH]
    pivot.pause_requested_via = actor.via
    pivot.pause_requested_at = timezone.now()
    pivot.pause_message = message
    pivot.save(update_fields=_PIVOT_SAVE_FIELDS)
    # The pivot event carries ``pause_pending``; the runner is nudged too.
    transaction.on_commit(lambda: broadcast_run_and_pivots(test_run, [pivot]))
    return CasePauseOutcome.REQUESTED


def _pause_waiting_person_step(
    project_id: int,
    test_run: TestRun,
    pivot: TestRunTestCase,
    step: CasePersonStep,
    actor: PersonStepActor,
    message: str,
) -> CasePauseOutcome:
    """Convert a waiting person step into a person's pause at once (all locked)."""
    now = timezone.now()
    step.status = PersonStepStatus.CANCELLED
    step.close_reason = PersonStepClose.PAUSED
    step.closed_by = actor.user
    step.closed_via = actor.via
    step.closed_at = now
    step.save(
        update_fields=["status", "close_reason", "closed_by", "closed_via", "closed_at"]
    )
    # Continue hands the step out afresh (or restores first for a restore
    # hand-off): the dropped key's hand-off is not reused.
    checkpoint_json = without_person_step(pivot.execution_checkpoint)
    pivot.execution_checkpoint = checkpoint_json
    help_request = open_pause_help_request(
        pivot,
        step_num=step.step_num,
        continue_mode=pause_continue_mode(
            checkpoint_from_json(checkpoint_json),
            restore_pending=step.kind == PersonStepKind.RESTORE,
        ),
        requested=PauseRequestSnapshot(
            user_id=actor.user.pk if actor.user is not None else None,
            label=actor.label(),
            via=actor.via,
            message=message,
        ),
        stopped_by_pause=True,
        base=None,
        now=now,
    )
    mark_case_paused_for_help(pivot, help_request)
    pivot.save(update_fields=["execution_checkpoint", *PAUSED_CASE_FIELDS])
    help_request_id = help_request.pk

    def _after_commit() -> None:
        broadcast_run_and_pivots(test_run, [pivot])
        broadcast_project_attention_changed(project_id)
        enqueue_open_paused_help_chat(help_request_id)

    transaction.on_commit(_after_commit)
    return CasePauseOutcome.PAUSED


def _can_pause(
    test_run: TestRun,
    pivot: TestRunTestCase,
    pending: bool,
    person_step_status: str | None,
    person_step_close_reason: str | None,
) -> tuple[bool, str]:
    """Whether Pause applies now, else the user-safe reason ("" while pending)."""
    if pending:
        return False, ""
    if test_run.status not in ACTIVE_RUN_STATUSES:
        return False, PAUSE_RUN_NOT_ACTIVE
    if pivot.status == TestRunTestCaseStatus.NEED_HELP:
        return False, PAUSE_ALREADY_NEEDS_HELP
    if (
        person_step_status == PersonStepStatus.REPORTED
        and person_step_close_reason in MARK_CLOSE_REASONS
    ):
        return False, PAUSE_MARK_RECORDING
    if (
        test_run.status == TestRunStatus.STARTED
        and pivot.status == TestRunTestCaseStatus.IN_PROGRESS
    ):
        return True, ""
    if (
        test_run.status == TestRunStatus.PAUSED
        and pivot.status == TestRunTestCaseStatus.AWAITING_PERSON
    ):
        if person_step_status == PersonStepStatus.WAITING:
            return True, ""
        if person_step_status == PersonStepStatus.NOT_CONFIRMED:
            return False, _NOT_CONFIRMED
    if person_step_status == PersonStepStatus.REPORTED:
        return False, PAUSE_STEP_BEING_CONFIRMED
    return False, PAUSE_NOT_RUNNING
