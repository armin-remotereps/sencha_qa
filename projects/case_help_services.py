"""Use cases that resolve a Need help case: a person's actions and the auto-fix end.

Imports ``run_reopen_services``, ``services`` and the leaf
``machine_setup.models`` / ``auto_fix_stop`` / ``turn_state`` modules, never
``machine_setup.services`` (that package imports this one).

Every action locks the project, then the run (the order Start and Resume use),
requires the run to be PAUSED, and resolves the case's open help request. The
remaining cases are then queued (the helped case first), unless the auto-fix
turn of the request is still live: it is asked to stop, and the run is queued
when it ends (``finish_case_auto_fix``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from django.db import transaction
from django.db.models import Prefetch, Q, QuerySet
from django.utils import timezone

from accounts.models import CustomUser
from agents.checkpoint import with_help
from agents.types import CheckpointHelp, HelpSource
from machine_setup.auto_fix_stop import request_auto_fix_stop
from machine_setup.models import (
    LIVE_TURN_STATUSES,
    SetupConversation,
    SetupConversationKind,
    SetupTurn,
)
from machine_setup.turn_state import (
    MACHINE_SETUP_RUNNING_MESSAGE,
    SETUP_TURN_STALE_AFTER,
)
from projects.case_help_notifications import notify_case_needs_help
from projects.exceptions import RunHelpError
from projects.models import (
    HELP_NOTE_MAX_LENGTH,
    BlockedReason,
    CaseHelpRequest,
    HelpAutoFixState,
    HelpResolution,
    Project,
    TestRun,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.run_reopen_services import (
    list_reopen_blockers,
    lock_project_and_run,
    queue_pending_cases,
)
from projects.services import broadcast_run_and_pivots

_NOT_PAUSED = "This run is not waiting for help."
_NO_OPEN_REQUEST = "This case no longer needs help. Reload the page."
_STILL_NEEDS_HELP = "A case of this run still needs help. Resolve it on its case page."
_AUTO_FIX_NOT_RUNNING = "The auto-fix is not running."

# Prefetched onto a help request: its auto-fix threads, newest first.
_THREADS_ATTR = "auto_fix_threads"


class HelpActionOutcome(StrEnum):
    """What happened to the run after a help request was resolved."""

    # The pending cases are queued: the run continues now.
    QUEUED = "queued"
    # The auto-fix turn was asked to stop; the run continues when it ends.
    WAITING_FOR_AUTO_FIX = "waiting_for_auto_fix"
    # A gate refused (runner offline, another turn live, ...); the run stays
    # paused until someone clicks "Continue run".
    WAITING_FOR_GATES = "waiting_for_gates"


@dataclass(frozen=True)
class HelpActionResult:
    outcome: HelpActionOutcome
    # User-safe reason for WAITING_FOR_GATES; empty otherwise.
    blocker: str = ""


@dataclass(frozen=True)
class HelpPanel:
    """The help state of a paused run, for its run page and the helped case page."""

    # The run's latest help request (open, or resolved while the run waits),
    # with ``test_run_test_case__test_case`` loaded.
    request: CaseHelpRequest
    # Latest auto-fix thread of the request; None when no thread was opened.
    thread: SetupConversation | None
    # The auto-fix turn of the request is queued or running (not stale).
    auto_fix_live: bool
    # Why the pending cases cannot be queued right now (the live auto-fix of
    # this request aside, since resolving the request stops it).
    blockers: tuple[str, ...]

    @property
    def is_open(self) -> bool:
        return self.request.is_open

    @property
    def can_continue(self) -> bool:
        """Continue needs the machine: refused while a gate fails."""
        return self.request.is_open and not self.blockers

    @property
    def can_continue_run(self) -> bool:
        """The request is resolved but queueing waits ("Continue run")."""
        return not self.request.is_open and not self.auto_fix_live

    @property
    def waiting_for_auto_fix_stop(self) -> bool:
        return not self.request.is_open and self.auto_fix_live


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


def _with_threads() -> Prefetch[str, QuerySet[SetupConversation], str]:
    return Prefetch(
        "setup_conversations",
        queryset=SetupConversation.objects.filter(kind=SetupConversationKind.AUTO_FIX)
        .only("id", "help_request_id", "fix_result", "fix_summary", "created_at")
        .order_by("-created_at", "-id"),
        to_attr=_THREADS_ATTR,
    )


def _latest_thread(request: CaseHelpRequest) -> SetupConversation | None:
    threads: list[SetupConversation] = getattr(request, _THREADS_ATTR, [])
    return threads[0] if threads else None


def get_help_panel(
    test_run: TestRun, project: Project, *, pivot_id: int | None = None
) -> HelpPanel | None:
    """The help panel of a PAUSED run; None (no queries) for any other status.

    With ``pivot_id``, None unless the run's latest request is that case's.
    """
    if test_run.status != TestRunStatus.PAUSED:
        return None
    request = (
        CaseHelpRequest.objects.filter(test_run_test_case__test_run_id=test_run.pk)
        .select_related("test_run_test_case__test_case")
        .prefetch_related(_with_threads())
        .order_by("-requested_at", "-id")
        .first()
    )
    if request is None:
        return None
    if pivot_id is not None and request.test_run_test_case_id != pivot_id:
        return None
    auto_fix_live = _auto_fix_turn_live(request.pk, timezone.now())
    blockers = list_reopen_blockers(test_run, project)
    if auto_fix_live:
        # The live turn is this request's auto-fix: resolving the request
        # stops it, so it does not block the actions.
        blockers = [b for b in blockers if b != MACHINE_SETUP_RUNNING_MESSAGE]
    return HelpPanel(
        request=request,
        thread=_latest_thread(request),
        auto_fix_live=auto_fix_live,
        blockers=tuple(blockers),
    )


def list_case_help_requests(pivot: TestRunTestCase) -> list[CaseHelpRequest]:
    """Every help request of the case, oldest first, with person and thread loaded."""
    return list(
        CaseHelpRequest.objects.filter(test_run_test_case_id=pivot.pk)
        .select_related("resolved_by")
        .prefetch_related(_with_threads())
        .order_by("requested_at", "id")
    )


def get_open_help_request(test_run: TestRun) -> CaseHelpRequest | None:
    """The run's open help request with its case loaded; None when none is open."""
    return (
        CaseHelpRequest.objects.filter(
            test_run_test_case__test_run_id=test_run.pk,
            resolution=HelpResolution.OPEN,
        )
        .select_related("test_run_test_case__test_case")
        .first()
    )


def get_run_case_for_help(
    pivot_id: int, test_run_id: int, project: Project
) -> TestRunTestCase | None:
    """The run case a help action targets, scoped to the project and run."""
    return (
        TestRunTestCase.objects.filter(
            pk=pivot_id, test_run_id=test_run_id, test_run__project=project
        )
        .only("id", "test_run_id")
        .first()
    )


def format_help_action_message(verb: str, result: HelpActionResult) -> str:
    """The flash message after Continue / Mark failed / Skip."""
    if result.outcome == HelpActionOutcome.WAITING_FOR_AUTO_FIX:
        return f"{verb}. The run continues as soon as the auto-fix agent stops."
    if result.outcome == HelpActionOutcome.WAITING_FOR_GATES:
        return (
            f"{verb}, but the run cannot continue yet: {result.blocker} "
            "Then use Continue run on the run page."
        )
    return f"{verb}. The run continues."


# ---------------------------------------------------------------------------
# Human actions
# ---------------------------------------------------------------------------


def continue_after_help(
    *, pivot: TestRunTestCase, user: CustomUser, note: str
) -> HelpActionResult:
    """The machine is fixed: the case re-runs its failing step from its checkpoint."""
    with transaction.atomic():
        project, test_run = _lock_paused_run(pivot.test_run_id)
        locked_pivot, request = _lock_open_request(test_run, pivot.pk)
        now = timezone.now()
        auto_fix_live = _auto_fix_turn_live(request.pk, now)
        blockers = list_reopen_blockers(test_run, project)
        if auto_fix_live:
            blockers = [b for b in blockers if b != MACHINE_SETUP_RUNNING_MESSAGE]
        if blockers:
            raise RunHelpError(blockers[0])

        note = _cap_note(note)
        _resolve(request, HelpResolution.CONTINUED, user, note, now, auto_fix_live)
        _set_continue_pending(locked_pivot, request, note, "human")
        return _queue_or_wait(test_run, request, locked_pivot, auto_fix_live)


def mark_failed_after_help(
    *, pivot: TestRunTestCase, user: CustomUser, note: str
) -> HelpActionResult:
    """A person judges the case Failed; the rest of the run continues."""
    with transaction.atomic():
        _, test_run = _lock_paused_run(pivot.test_run_id)
        locked_pivot, request = _lock_open_request(test_run, pivot.pk)
        now = timezone.now()
        auto_fix_live = _auto_fix_turn_live(request.pk, now)
        note = _cap_note(note)
        _resolve(request, HelpResolution.MARKED_FAILED, user, note, now, auto_fix_live)
        locked_pivot.status = TestRunTestCaseStatus.FAILED
        locked_pivot.blocked_reason = ""
        locked_pivot.result = _verdict_text(
            f"Marked failed by {_user_label(user)} at step {request.step_num} "
            "while it was waiting for help.",
            request,
            note,
        )
        locked_pivot.finished_at = now
        locked_pivot.save(
            update_fields=[
                "status",
                "blocked_reason",
                "result",
                "finished_at",
                "updated_at",
            ]
        )
        return _queue_or_wait(test_run, request, locked_pivot, auto_fix_live)


def skip_after_help(
    *, pivot: TestRunTestCase, user: CustomUser, note: str
) -> HelpActionResult:
    """Leave the case Blocked (resumable later); the rest of the run continues."""
    with transaction.atomic():
        _, test_run = _lock_paused_run(pivot.test_run_id)
        locked_pivot, request = _lock_open_request(test_run, pivot.pk)
        now = timezone.now()
        auto_fix_live = _auto_fix_turn_live(request.pk, now)
        note = _cap_note(note)
        _resolve(request, HelpResolution.SKIPPED, user, note, now, auto_fix_live)
        locked_pivot.status = TestRunTestCaseStatus.BLOCKED
        locked_pivot.blocked_reason = BlockedReason.HELP_SKIPPED
        locked_pivot.result = _verdict_text(
            f"Skipped by {_user_label(user)} at step {request.step_num} "
            "while it was waiting for help.",
            request,
            note,
        )
        locked_pivot.finished_at = now
        locked_pivot.save(
            update_fields=[
                "status",
                "blocked_reason",
                "result",
                "finished_at",
                "updated_at",
            ]
        )
        return _queue_or_wait(test_run, request, locked_pivot, auto_fix_live)


def stop_auto_fix(*, pivot: TestRunTestCase, user: CustomUser) -> None:
    """Ask the running auto-fix of the case to stop; a person takes over.

    The turn stops at its next tool boundary and its end settles the request
    (not fixed: the members are notified). When no turn exists yet (the start
    task is still queued), the request falls back to a person at once.
    """
    with transaction.atomic():
        _, test_run = _lock_paused_run(pivot.test_run_id)
        locked_pivot, request = _lock_open_request(test_run, pivot.pk)
        if request.auto_fix_state != HelpAutoFixState.RUNNING:
            raise RunHelpError(_AUTO_FIX_NOT_RUNNING)
        if request_auto_fix_stop(request.pk):
            return
        request.auto_fix_state = HelpAutoFixState.NOT_FIXED
        request.save(update_fields=["auto_fix_state"])
        transaction.on_commit(
            lambda: broadcast_run_and_pivots(test_run, [locked_pivot])
        )


def continue_paused_run(*, test_run: TestRun, user: CustomUser) -> None:
    """Queue the pending cases of a paused run whose help request is resolved.

    For when queueing failed its gates at resolution time (the runner was
    offline when the auto-fix ended, for example).
    """
    with transaction.atomic():
        _, locked_run = _lock_paused_run(test_run.pk)
        if CaseHelpRequest.objects.filter(
            test_run_test_case__test_run_id=locked_run.pk,
            resolution=HelpResolution.OPEN,
        ).exists():
            raise RunHelpError(_STILL_NEEDS_HELP)
        latest_id = (
            CaseHelpRequest.objects.filter(
                test_run_test_case__test_run_id=locked_run.pk
            )
            .order_by("-requested_at", "-id")
            .values_list("id", flat=True)
            .first()
        )
        if latest_id is not None and _auto_fix_turn_live(latest_id, timezone.now()):
            raise RunHelpError("The run continues as soon as the auto-fix agent stops.")
        queue_pending_cases(locked_run)


# ---------------------------------------------------------------------------
# Auto-fix end (called by machine_setup)
# ---------------------------------------------------------------------------


def finish_case_auto_fix(*, help_request_id: int, fixed: bool, summary: str) -> None:
    """Settle a help request once its auto-fix setup turn has ended.

    Called by ``machine_setup`` after the turn's status is final. Locks project
    then run and does nothing unless the run is PAUSED. Fixed: the request is
    resolved ``auto_fixed`` and the pending cases are queued (the helped case
    first). Not fixed, or stopped: the request stays open for a person and the
    project members are notified. If a person already resolved the request
    during the auto-fix, the pending cases are queued.
    """
    test_run_id = (
        CaseHelpRequest.objects.filter(pk=help_request_id)
        .values_list("test_run_test_case__test_run_id", flat=True)
        .first()
    )
    if test_run_id is None:
        return
    final_state = HelpAutoFixState.FIXED if fixed else HelpAutoFixState.NOT_FIXED
    with transaction.atomic():
        _, test_run = lock_project_and_run(test_run_id)
        if test_run.status != TestRunStatus.PAUSED:
            # Aborted, or already queued by a person: only record the outcome
            # (Abort leaves the state "running"); no queue, no notification.
            CaseHelpRequest.objects.filter(
                pk=help_request_id, auto_fix_state=HelpAutoFixState.RUNNING
            ).update(auto_fix_state=final_state)
            return
        request = CaseHelpRequest.objects.select_for_update().get(pk=help_request_id)
        pivot = (
            TestRunTestCase.objects.select_for_update()
            .defer("logs")
            .get(pk=request.test_run_test_case_id)
        )
        # The run event carries ``help_state``, so the run page sees the change.
        transaction.on_commit(lambda: broadcast_run_and_pivots(test_run, [pivot]))

        if not request.is_open:
            # A person resolved it during the auto-fix; the run waited for the
            # turn to end. A failing gate leaves "Continue run" on the page.
            if request.auto_fix_state == HelpAutoFixState.RUNNING:
                request.auto_fix_state = final_state
                request.save(update_fields=["auto_fix_state"])
            _try_queue(test_run)
            return

        if not fixed:
            request.auto_fix_state = HelpAutoFixState.NOT_FIXED
            request.save(update_fields=["auto_fix_state"])
            notify_case_needs_help(request.pk, after_auto_fix=True)
            return

        note = _cap_note(summary)
        try:
            with transaction.atomic():
                request.auto_fix_state = HelpAutoFixState.FIXED
                request.resolution = HelpResolution.AUTO_FIXED
                request.resolved_by = None
                request.resolved_at = timezone.now()
                request.note = note
                request.save(
                    update_fields=[
                        "auto_fix_state",
                        "resolution",
                        "resolved_by",
                        "resolved_at",
                        "note",
                    ]
                )
                _set_continue_pending(pivot, request, note, "auto_fix")
                queue_pending_cases(test_run)
        except RunHelpError:
            # The gates refused (runner gone, ...): back to a person, with the
            # agent's report kept on the request.
            _reload_after_rollback(test_run, pivot)
            CaseHelpRequest.objects.filter(pk=request.pk).update(
                auto_fix_state=HelpAutoFixState.FIXED
            )
            notify_case_needs_help(request.pk)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lock_paused_run(test_run_id: int) -> tuple[Project, TestRun]:
    project, test_run = lock_project_and_run(test_run_id)
    if test_run.status != TestRunStatus.PAUSED:
        raise RunHelpError(_NOT_PAUSED)
    return project, test_run


def _lock_open_request(
    test_run: TestRun, pivot_id: int
) -> tuple[TestRunTestCase, CaseHelpRequest]:
    pivot = (
        TestRunTestCase.objects.select_for_update()
        .defer("logs")
        .filter(pk=pivot_id, test_run_id=test_run.pk)
        .first()
    )
    if pivot is None or pivot.status != TestRunTestCaseStatus.NEED_HELP:
        raise RunHelpError(_NO_OPEN_REQUEST)
    request = (
        CaseHelpRequest.objects.select_for_update()
        .filter(test_run_test_case_id=pivot.pk, resolution=HelpResolution.OPEN)
        .first()
    )
    if request is None:
        raise RunHelpError(_NO_OPEN_REQUEST)
    return pivot, request


def _auto_fix_turn_live(help_request_id: int, now: datetime) -> bool:
    """Whether an auto-fix turn of the request is queued or running (not stale)."""
    cutoff = now - SETUP_TURN_STALE_AFTER
    return SetupTurn.objects.filter(
        Q(started_at__gte=cutoff) | Q(started_at__isnull=True, created_at__gte=cutoff),
        auto_fix=True,
        conversation__help_request_id=help_request_id,
        status__in=LIVE_TURN_STATUSES,
    ).exists()


def _resolve(
    request: CaseHelpRequest,
    resolution: HelpResolution,
    user: CustomUser,
    note: str,
    now: datetime,
    auto_fix_live: bool,
) -> None:
    request.resolution = resolution
    request.resolved_by = user
    request.resolved_at = now
    request.note = note
    update_fields = ["resolution", "resolved_by", "resolved_at", "note"]
    if request.auto_fix_state == HelpAutoFixState.RUNNING and not auto_fix_live:
        # The auto-fix never got a live turn, or its worker died: it did not
        # fix the machine. A live turn reports its own result when it ends.
        request.auto_fix_state = HelpAutoFixState.NOT_FIXED
        update_fields.append("auto_fix_state")
    request.save(update_fields=update_fields)


def _set_continue_pending(
    pivot: TestRunTestCase, request: CaseHelpRequest, note: str, source: HelpSource
) -> None:
    """Write the checkpoint's help key and put the case back to Created."""
    pivot.execution_checkpoint = with_help(
        pivot.execution_checkpoint,
        CheckpointHelp(
            step_num=request.step_num,
            request_id=request.pk,
            note=note,
            source=source,
            continue_pending=True,
        ),
    )
    pivot.status = TestRunTestCaseStatus.CREATED
    pivot.blocked_reason = ""
    pivot.result = ""
    pivot.finished_at = None
    pivot.save(
        update_fields=[
            "execution_checkpoint",
            "status",
            "blocked_reason",
            "result",
            "finished_at",
            "updated_at",
        ]
    )


def _queue_or_wait(
    test_run: TestRun,
    request: CaseHelpRequest,
    pivot: TestRunTestCase,
    auto_fix_live: bool,
) -> HelpActionResult:
    # Registered before the queue's own callbacks, so a broker failure that
    # cancels the run is the last word on the page.
    transaction.on_commit(lambda: broadcast_run_and_pivots(test_run, [pivot]))
    if auto_fix_live and request_auto_fix_stop(request.pk):
        return HelpActionResult(HelpActionOutcome.WAITING_FOR_AUTO_FIX)
    blocker = _try_queue(test_run)
    if blocker:
        return HelpActionResult(HelpActionOutcome.WAITING_FOR_GATES, blocker)
    return HelpActionResult(HelpActionOutcome.QUEUED)


def _try_queue(test_run: TestRun) -> str:
    """Queue the pending cases; the refusal's message, or "" when queued.

    A refusal rolls back only the queueing (a savepoint), so the caller's
    resolution is kept and the run stays paused.
    """
    try:
        with transaction.atomic():
            queue_pending_cases(test_run)
    except RunHelpError as exc:
        test_run.refresh_from_db(
            fields=["status", "execution_generation", "finished_at"]
        )
        return exc.message
    return ""


def _reload_after_rollback(test_run: TestRun, pivot: TestRunTestCase) -> None:
    test_run.refresh_from_db(fields=["status", "execution_generation", "finished_at"])
    pivot.refresh_from_db(
        fields=["status", "blocked_reason", "result", "execution_checkpoint"]
    )


def _cap_note(note: str) -> str:
    return note.strip()[:HELP_NOTE_MAX_LENGTH]


def _user_label(user: CustomUser) -> str:
    return user.get_full_name() or user.email


def _verdict_text(headline: str, request: CaseHelpRequest, note: str) -> str:
    parts = [headline]
    if request.ask:
        parts.append(f"The agent had asked: {request.ask}")
    if note:
        parts.append(f"Note: {note}")
    return "\n".join(parts)
