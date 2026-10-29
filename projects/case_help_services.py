"""Use cases that resolve a Need help case: a person's actions and the auto-fix end.

Imports ``run_reopen_services``, ``services`` and the leaf
``machine_setup.models`` / ``auto_fix_stop`` / ``turn_state`` modules, never
``machine_setup.services`` (that package imports this one).

Every action locks the project, then the run (the order Start and Resume use),
then the help request, then (Continue / Mark failed / Mark blocked) the case's open
person steps, then the case (the order every help writer uses; the person
step services also lock a step before its case),
requires the run to be PAUSED, and resolves the case's open help request. The
remaining cases are then queued (the helped case first), unless the auto-fix
turn of the request is still live: it is asked to stop, and the run is queued
when it ends (``finish_case_auto_fix``).

A help-chat redesign of the plan (``case_plan_revision_services``) is applied
by Continue when the person keeps it; every other resolution supersedes it.
Every resolution clears the help-chat agent's suggestion.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from django.db import transaction
from django.db.models import Prefetch, QuerySet, Subquery
from django.utils import timezone

from accounts.models import CustomUser
from agents.checkpoint import (
    read_checkpoint_person_step,
    with_help,
    without_person_step,
)
from agents.plan_revision import PlanDraftView
from agents.types import CheckpointHelp, HelpSource
from machine_setup.auto_fix_stop import request_auto_fix_stop
from machine_setup.models import SetupConversation, SetupConversationKind
from machine_setup.turn_state import (
    MACHINE_SETUP_RUNNING_MESSAGE,
    auto_fix_turn_live,
    person_turn_live,
)
from notifications.services.inbox_broadcast import broadcast_project_attention_changed
from projects.case_help_notifications import notify_case_needs_help
from projects.case_help_proposal_services import clear_agent_proposal
from projects.case_plan_revision_services import (
    apply_plan_revision_locked,
    get_pending_plan_draft,
    pending_revision_id_locked,
    supersede_pending_revisions,
    with_decided_revisions,
)
from projects.exceptions import RunHelpError
from projects.models import (
    HELP_NOTE_MAX_LENGTH,
    OPEN_PERSON_STEP_STATUSES,
    BlockedReason,
    CaseHelpRequest,
    CasePersonStep,
    FailedBy,
    HelpAutoFixState,
    HelpContinueMode,
    HelpResolution,
    PersonStepClose,
    PersonStepStatus,
    Project,
    TestRun,
    TestRunScreenshot,
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
from test_sessions.case_activity import CaseActivity
from test_sessions.services.record_case_activity import record_case_activity

_NOT_PAUSED = "This run is not waiting for help."
_NO_OPEN_REQUEST = "This case no longer needs help. Reload the page."
_STILL_NEEDS_HELP = "A case of this run still needs help. Resolve it on its case page."
_AUTO_FIX_NOT_RUNNING = "The auto-fix is not running."
_AGENT_REPLYING = "The agent is still replying. Wait or press Stop."
_NEWER_DRAFT = "The agent proposed a newer plan. Review it, then continue again."

# Prefetched onto a help request: its threads (auto-fix or help chat), newest
# first. The one-thread-per-request constraint keeps it to one row.
_THREADS_ATTR = "help_threads"
_HELP_THREAD_KINDS = (SetupConversationKind.AUTO_FIX, SetupConversationKind.CASE_HELP)


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
    # The request's thread (Night auto-fix or help chat); None when no thread
    # was opened.
    thread: SetupConversation | None
    # The auto-fix turn of the request is queued or running (not stale).
    auto_fix_live: bool
    # Why the pending cases cannot be queued right now (the live auto-fix of
    # this request aside, since resolving the request stops it).
    blockers: tuple[str, ...]
    # How Continue resumes the case: from the current screen, after restoring
    # the earlier steps' state, or from scratch (no usable checkpoint).
    continue_mode: HelpContinueMode
    # The help-chat agent's pending redesign of the plan (open request only).
    pending_draft: PlanDraftView | None = None
    # The case's newest screenshot ("" when none).
    latest_screenshot_url: str = ""

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

    @property
    def auto_fix_summary(self) -> str:
        """The setup agent's report: the request's copy, else the thread's."""
        if self.request.auto_fix_summary:
            return self.request.auto_fix_summary
        return self.thread.fix_summary if self.thread is not None else ""


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


def _with_threads() -> Prefetch[str, QuerySet[SetupConversation], str]:
    return Prefetch(
        "setup_conversations",
        queryset=SetupConversation.objects.filter(kind__in=_HELP_THREAD_KINDS)
        .only(
            "id",
            "help_request_id",
            "kind",
            "archived_at",
            "fix_result",
            "fix_summary",
            "created_at",
        )
        .order_by("-created_at", "-id"),
        to_attr=_THREADS_ATTR,
    )


def _latest_thread(request: CaseHelpRequest) -> SetupConversation | None:
    threads: list[SetupConversation] = getattr(request, _THREADS_ATTR, [])
    return threads[0] if threads else None


def get_help_panel(
    test_run: TestRun,
    project: Project,
    *,
    pivot_id: int | None = None,
    blockers: Sequence[str] | None = None,
) -> HelpPanel | None:
    """The help panel of a PAUSED run; None (no queries) for any other status.

    With ``pivot_id``, None unless the run's latest request is that case's.
    ``blockers``: the run's reopen gates (``list_reopen_blockers``) when the
    caller already computed them; computed here otherwise.
    """
    if test_run.status != TestRunStatus.PAUSED:
        return None
    request = (
        CaseHelpRequest.objects.filter(test_run_test_case__test_run_id=test_run.pk)
        .select_related("test_run_test_case__test_case")
        .defer("test_run_test_case__logs")
        .prefetch_related(_with_threads())
        .order_by("-requested_at", "-id")
        .first()
    )
    if request is None:
        return None
    if pivot_id is not None and request.test_run_test_case_id != pivot_id:
        return None
    auto_fix_live = auto_fix_turn_live(request.pk)
    if blockers is None:
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
        continue_mode=_continue_mode(request),
        pending_draft=get_pending_plan_draft(request.pk) if request.is_open else None,
        latest_screenshot_url=_latest_screenshot_url(request.test_run_test_case_id),
    )


def list_case_help_requests(pivot: TestRunTestCase) -> list[CaseHelpRequest]:
    """Every help request of the case, oldest first.

    Loaded: the resolving person, the thread (``help_threads``) and the
    applied and discarded redesigns (``decided_plan_revisions``).
    """
    return list(
        CaseHelpRequest.objects.filter(test_run_test_case_id=pivot.pk)
        .select_related("resolved_by")
        .prefetch_related(_with_threads(), with_decided_revisions())
        .order_by("requested_at", "id")
    )


def resolve_return_thread(
    *, pivot: TestRunTestCase, thread_id: int | None
) -> int | None:
    """``thread_id`` if it is the non-archived thread of the case's latest request.

    The help actions redirect there; any other id falls back to the case page.
    """
    if thread_id is None:
        return None
    latest_request = (
        CaseHelpRequest.objects.filter(test_run_test_case_id=pivot.pk)
        .order_by("-requested_at", "-id")
        .values("id")[:1]
    )
    return (
        SetupConversation.objects.filter(
            pk=thread_id,
            archived_at__isnull=True,
            help_request_id=Subquery(latest_request),
        )
        .values_list("id", flat=True)
        .first()
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
    """The flash message after Continue / Mark failed / Mark blocked."""
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
    *,
    pivot: TestRunTestCase,
    user: CustomUser,
    note: str,
    plan_revision_id: int | None = None,
    use_plan_revision: bool = True,
) -> HelpActionResult:
    """The machine is fixed: the case continues per its ``HelpContinueMode``.

    It re-runs its failing step from the current screen, restores the earlier
    steps' state first (the runner was lost), or plans from scratch (no
    usable checkpoint).

    ``plan_revision_id`` is the help-chat redesign the person saw (None when
    none was shown). With ``use_plan_revision`` that draft is applied first and
    the case resumes at its first step that is not carried; otherwise a
    pending draft is superseded and the case re-runs its failing step. A
    pending draft other than the one shown refuses (the agent proposed a newer
    plan).

    A person step a fault left open (Human in the loop) is cancelled unless
    the checkpoint keeps its key (``_write_help_key``): the step is handed out
    afresh, not as a rejected report.
    """
    with transaction.atomic():
        project, test_run = _lock_paused_run(pivot.test_run_id)
        request = _lock_request(pivot.pk)
        person_step_ids = _lock_open_person_step_ids(pivot.pk)
        locked_pivot = _lock_help_case(test_run, pivot.pk)
        now = timezone.now()
        if person_turn_live(request.pk):
            raise RunHelpError(_AGENT_REPLYING)
        auto_fix_live = auto_fix_turn_live(request.pk)
        blockers = list_reopen_blockers(test_run, project)
        if auto_fix_live:
            blockers = [b for b in blockers if b != MACHINE_SETUP_RUNNING_MESSAGE]
        if blockers:
            raise RunHelpError(blockers[0])

        if use_plan_revision and plan_revision_id is not None:
            apply_plan_revision_locked(
                pivot=locked_pivot,
                request=request,
                revision_id=plan_revision_id,
                user=user,
            )
        else:
            pending_id = pending_revision_id_locked(request)
            if pending_id is not None and pending_id != plan_revision_id:
                raise RunHelpError(_NEWER_DRAFT)
            supersede_pending_revisions(request)

        note = _cap_note(note)
        _resolve(
            request,
            HelpResolution.CONTINUED,
            user,
            note,
            now,
            auto_fix_live,
            project_id=test_run.project_id,
        )
        _set_continue_pending(locked_pivot, request, "human")
        if read_checkpoint_person_step(locked_pivot.execution_checkpoint) is None:
            _cancel_person_steps(person_step_ids, PersonStepClose.OPEN, user, now)
        return _queue_or_wait(test_run, request, locked_pivot, auto_fix_live)


def mark_failed_after_help(
    *, pivot: TestRunTestCase, user: CustomUser, note: str
) -> HelpActionResult:
    """A person judges the case Failed; the rest of the run continues."""
    with transaction.atomic():
        _, test_run = _lock_paused_run(pivot.test_run_id)
        request = _lock_request(pivot.pk)
        person_step_ids = _lock_open_person_step_ids(pivot.pk)
        locked_pivot = _lock_help_case(test_run, pivot.pk)
        now = timezone.now()
        auto_fix_live = auto_fix_turn_live(request.pk)
        note = _cap_note(note)
        # Human in the loop: a step left open on the case closes with it.
        _cancel_person_steps(person_step_ids, PersonStepClose.MARKED_FAILED, user, now)
        supersede_pending_revisions(request)
        _resolve(
            request,
            HelpResolution.MARKED_FAILED,
            user,
            note,
            now,
            auto_fix_live,
            project_id=test_run.project_id,
        )
        locked_pivot.status = TestRunTestCaseStatus.FAILED
        locked_pivot.failed_by = FailedBy.HELP
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
                "failed_by",
                "blocked_reason",
                "result",
                "finished_at",
                "updated_at",
            ]
        )
        return _queue_or_wait(test_run, request, locked_pivot, auto_fix_live)


def mark_blocked_after_help(
    *, pivot: TestRunTestCase, user: CustomUser, note: str
) -> HelpActionResult:
    """A person marks the case Blocked (resumable later); the run continues.

    The only writer of ``BLOCKED``: the reason is always ``HELP_SKIPPED``, and
    the request's fault, if any, appears only in the result text.
    """
    with transaction.atomic():
        _, test_run = _lock_paused_run(pivot.test_run_id)
        request = _lock_request(pivot.pk)
        person_step_ids = _lock_open_person_step_ids(pivot.pk)
        locked_pivot = _lock_help_case(test_run, pivot.pk)
        now = timezone.now()
        auto_fix_live = auto_fix_turn_live(request.pk)
        note = _cap_note(note)
        # Human in the loop: a step left open on the case closes with it.
        _cancel_person_steps(person_step_ids, PersonStepClose.MARKED_BLOCKED, user, now)
        # The blocked case resumes later on its current (old) plan.
        supersede_pending_revisions(request)
        _resolve(
            request,
            HelpResolution.SKIPPED,
            user,
            note,
            now,
            auto_fix_live,
            project_id=test_run.project_id,
        )
        locked_pivot.status = TestRunTestCaseStatus.BLOCKED
        locked_pivot.blocked_reason = BlockedReason.HELP_SKIPPED
        fault = f" ({_fault_label(request)})" if request.fault else ""
        locked_pivot.result = _verdict_text(
            f"Marked blocked by {_user_label(user)} at step {request.step_num} "
            f"while it was waiting for help{fault}.",
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
    (not fixed: the members are notified then). When no turn exists yet (the
    start task is still queued), the request falls back to a person at once
    and the members are notified on commit; the late start task is a no-op.
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
        request_id = request.pk
        project_id = test_run.project_id

        def _after_commit() -> None:
            broadcast_run_and_pivots(test_run, [locked_pivot])
            broadcast_project_attention_changed(project_id)
            notify_case_needs_help(request_id, after_auto_fix=True)

        transaction.on_commit(_after_commit)


def lock_help_request_for_chat(*, project: Project, pivot_id: int) -> CaseHelpRequest:
    """Lock the case's open help request for opening its help chat.

    Must be called inside ``transaction.atomic()``. Locks project, run, request
    and case; raises ``RunHelpError`` unless the run is PAUSED with an open
    request for that case of ``project``. A running auto-fix that has no
    thread yet (its start task is still queued) is released to the person:
    it becomes not fixed without a notification (the person is here), and
    the late start task does nothing. Returns the request with
    ``test_run_test_case`` loaded.
    """
    test_run_id = (
        TestRunTestCase.objects.filter(pk=pivot_id, test_run__project=project)
        .values_list("test_run_id", flat=True)
        .first()
    )
    if test_run_id is None:
        raise RunHelpError(_NO_OPEN_REQUEST)
    _, test_run = _lock_paused_run(test_run_id)
    locked_pivot, request = _lock_open_request(test_run, pivot_id)
    request.test_run_test_case = locked_pivot
    if (
        request.auto_fix_state == HelpAutoFixState.RUNNING
        and not SetupConversation.objects.filter(help_request_id=request.pk).exists()
    ):
        request.auto_fix_state = HelpAutoFixState.NOT_FIXED
        request.save(update_fields=["auto_fix_state"])
        transaction.on_commit(
            lambda: broadcast_run_and_pivots(test_run, [locked_pivot])
        )
        transaction.on_commit(lambda: broadcast_project_attention_changed(project.pk))
    return request


def restore_open_help_after_runner_update(project_id: int) -> int:
    """Switch the project's open current-screen help requests to restore.

    Called when a Test Runner update succeeded, inside a transaction that
    already holds the project row lock: the handover closed the browsers and
    apps the old runner opened, so the screen a paused case stopped at is gone.
    A current-screen request always has a checkpoint (no checkpoint means from
    scratch), so Continue can restore the earlier steps instead. Locks the
    project's paused runs (in id order) before writing their requests, the
    order every help action uses. Returns the number of requests switched.
    """
    run_ids = list(
        TestRun.objects.select_for_update()
        .filter(project_id=project_id, status=TestRunStatus.PAUSED)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    if not run_ids:
        return 0
    return CaseHelpRequest.objects.filter(
        resolution=HelpResolution.OPEN,
        continue_mode=HelpContinueMode.CURRENT_SCREEN,
        test_run_test_case__test_run_id__in=run_ids,
    ).update(continue_mode=HelpContinueMode.RESTORE)


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
        if latest_id is not None and auto_fix_turn_live(latest_id):
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
    during the auto-fix, the pending cases are queued. An open request whose
    auto-fix is no longer ``running`` (already settled, or stopped before a
    turn existed) only gets the summary: no second notification, no continue.
    """
    test_run_id = (
        CaseHelpRequest.objects.filter(pk=help_request_id)
        .values_list("test_run_test_case__test_run_id", flat=True)
        .first()
    )
    if test_run_id is None:
        return
    final_state = HelpAutoFixState.FIXED if fixed else HelpAutoFixState.NOT_FIXED
    # Kept on the request for both outcomes: the tester and the person who
    # continues see what the setup agent did. ``note`` stays the person's.
    auto_fix_summary = _cap_note(summary)
    with transaction.atomic():
        _, test_run = lock_project_and_run(test_run_id)
        if test_run.status != TestRunStatus.PAUSED:
            # Aborted, or already queued by a person: only record the outcome
            # (Abort leaves the state "running"); no queue, no notification.
            CaseHelpRequest.objects.filter(
                pk=help_request_id, auto_fix_state=HelpAutoFixState.RUNNING
            ).update(auto_fix_state=final_state, auto_fix_summary=auto_fix_summary)
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
                request.auto_fix_summary = auto_fix_summary
                request.save(update_fields=["auto_fix_state", "auto_fix_summary"])
                if (
                    request.resolution == HelpResolution.CONTINUED
                    and pivot.status == TestRunTestCaseStatus.CREATED
                ):
                    # The person's Continue wrote the help key before the
                    # agent reported; add its summary for the tester.
                    _write_help_key(pivot, request, "human")
                    pivot.save(update_fields=["execution_checkpoint", "updated_at"])
            _try_queue(test_run)
            return

        request.auto_fix_summary = auto_fix_summary
        if request.auto_fix_state != HelpAutoFixState.RUNNING:
            # Already settled while the turn was ending: a person pressed Stop
            # before a turn existed (not fixed, notified), or an earlier settle
            # of this request ran. Keep the agent's report only: no second
            # notification and no auto-continue.
            request.save(update_fields=["auto_fix_summary"])
            return
        if not fixed:
            request.auto_fix_state = HelpAutoFixState.NOT_FIXED
            request.save(update_fields=["auto_fix_state", "auto_fix_summary"])
            notify_case_needs_help(request.pk, after_auto_fix=True)
            # "Auto-fix is trying" ends: members' attention lists change.
            not_fixed_project_id = test_run.project_id
            transaction.on_commit(
                lambda: broadcast_project_attention_changed(not_fixed_project_id)
            )
            return

        try:
            with transaction.atomic():
                request.auto_fix_state = HelpAutoFixState.FIXED
                request.resolution = HelpResolution.AUTO_FIXED
                request.resolved_by = None
                request.resolved_at = timezone.now()
                request.save(
                    update_fields=[
                        "auto_fix_state",
                        "auto_fix_summary",
                        "resolution",
                        "resolved_by",
                        "resolved_at",
                    ]
                )
                supersede_pending_revisions(request)
                clear_agent_proposal(request)
                _set_continue_pending(pivot, request, "auto_fix")
                queue_pending_cases(test_run)
                project_id = test_run.project_id
                # The help request closed: members' attention lists change.
                transaction.on_commit(
                    lambda: broadcast_project_attention_changed(project_id)
                )
        except RunHelpError:
            # The gates refused (runner gone, ...): back to a person, with the
            # agent's report kept on the request.
            _reload_after_rollback(test_run, pivot)
            CaseHelpRequest.objects.filter(pk=request.pk).update(
                auto_fix_state=HelpAutoFixState.FIXED,
                auto_fix_summary=auto_fix_summary,
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
    """Lock the case's open help request, then the case (after project and run).

    The order of ``case_plan_revision_services.lock_waiting_help_request``.
    Raises ``RunHelpError`` unless the request is open and the case Need help.
    """
    request = _lock_request(pivot_id)
    return _lock_help_case(test_run, pivot_id), request


def _lock_request(pivot_id: int) -> CaseHelpRequest:
    """Lock the case's open help request; ``RunHelpError`` when none is open."""
    request = (
        CaseHelpRequest.objects.select_for_update()
        .filter(test_run_test_case_id=pivot_id, resolution=HelpResolution.OPEN)
        .first()
    )
    if request is None:
        raise RunHelpError(_NO_OPEN_REQUEST)
    return request


def _lock_help_case(test_run: TestRun, pivot_id: int) -> TestRunTestCase:
    """Lock the case (last); ``RunHelpError`` unless it is in Need help."""
    pivot = (
        TestRunTestCase.objects.select_for_update()
        .defer("logs")
        .filter(pk=pivot_id, test_run_id=test_run.pk)
        .first()
    )
    if pivot is None or pivot.status != TestRunTestCaseStatus.NEED_HELP:
        raise RunHelpError(_NO_OPEN_REQUEST)
    return pivot


def _lock_open_person_step_ids(pivot_id: int) -> list[int]:
    """Lock the case's open person steps (after the request, before the case).

    A fault while the agent confirmed a person's report leaves the step
    open (``reported``) while the case needs help; one row at most.
    """
    return list(
        CasePersonStep.objects.select_for_update()
        .filter(test_run_test_case_id=pivot_id, status__in=OPEN_PERSON_STEP_STATUSES)
        .values_list("id", flat=True)
    )


def _cancel_person_steps(
    person_step_ids: list[int],
    close_reason: PersonStepClose,
    user: CustomUser,
    now: datetime,
) -> None:
    """Close the locked open person steps: the case finished without them."""
    if not person_step_ids:
        return
    CasePersonStep.objects.filter(pk__in=person_step_ids).update(
        status=PersonStepStatus.CANCELLED,
        close_reason=close_reason,
        closed_by=user,
        closed_at=now,
    )


def _resolve(
    request: CaseHelpRequest,
    resolution: HelpResolution,
    user: CustomUser,
    note: str,
    now: datetime,
    auto_fix_live: bool,
    *,
    project_id: int,
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
    clear_agent_proposal(request)
    record_case_activity(
        user=user,
        pivot_ids=[request.test_run_test_case_id],
        activity=CaseActivity.HELP,
    )
    # The help request closed: the project members' attention lists change.
    transaction.on_commit(lambda: broadcast_project_attention_changed(project_id))


def _latest_screenshot_url(pivot_id: int) -> str:
    screenshot = (
        TestRunScreenshot.objects.filter(test_run_test_case_id=pivot_id)
        .only("id", "image")
        .order_by("-created_at", "-id")
        .first()
    )
    return screenshot.image.url if screenshot is not None and screenshot.image else ""


def _continue_mode(request: CaseHelpRequest) -> HelpContinueMode:
    # Decided when the case paused; see ``projects.case_faults.help_continue_mode``.
    return HelpContinueMode(request.continue_mode)


def _fault_label(request: CaseHelpRequest) -> str:
    """The fault's display label; empty when the agent asked for help."""
    return request.get_fault_display() if request.fault else ""


def _write_help_key(
    pivot: TestRunTestCase, request: CaseHelpRequest, source: HelpSource
) -> None:
    """Prepare the checkpoint for continuing, per ``HelpContinueMode`` (not saved).

    From scratch (no usable checkpoint): the checkpoint becomes ``{}`` with no
    help key, so the case plans again after the normal cleanup. Restore: the
    help key is written with ``continue_pending`` off, so the normal resume
    runs the cleanup and the restore sub-task before the step. Current
    screen: ``continue_pending`` on, so both are skipped.

    The help key carries the ask, the person's note (empty when the setup
    agent continued), the setup agent's summary (empty when no auto-fix
    reported), whether that auto-fix reported the machine fixed, and the
    fault's label (empty when the agent asked for help).

    A person step key (Human in the loop) survives only a current-screen
    Continue, the retry of a confirmation a fault interrupted: after a restore
    a report made before it must not be confirmed, so the step is handed out
    afresh.
    """
    mode = _continue_mode(request)
    if mode == HelpContinueMode.FROM_SCRATCH:
        pivot.execution_checkpoint = {}
        return
    checkpoint = pivot.execution_checkpoint
    if mode != HelpContinueMode.CURRENT_SCREEN:
        checkpoint = without_person_step(checkpoint)
    pivot.execution_checkpoint = with_help(
        checkpoint,
        CheckpointHelp(
            step_num=request.step_num,
            request_id=request.pk,
            note=request.note,
            source=source,
            continue_pending=mode == HelpContinueMode.CURRENT_SCREEN,
            ask=request.ask,
            auto_fix_summary=request.auto_fix_summary,
            auto_fix_fixed=request.auto_fix_state == HelpAutoFixState.FIXED,
            fault=_fault_label(request),
        ),
    )


def _set_continue_pending(
    pivot: TestRunTestCase, request: CaseHelpRequest, source: HelpSource
) -> None:
    """Prepare the checkpoint (``_write_help_key``) and put the case back to Created.

    Works on the in-memory ``execution_checkpoint`` (a redesign
    applied just before is only assigned there) and saves the case once.
    """
    _write_help_key(pivot, request, source)
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
