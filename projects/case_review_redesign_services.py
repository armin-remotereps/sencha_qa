"""Use cases for a result-chat redesign of a finished case's test plan.

The result-chat agent proposes a full new step list whose leading steps may be
carried from steps that passed (``propose_review_plan_revision``); it is stored
as the case's single pending review draft (``CasePlanRevision`` with
``source=review`` and no help request). A person discards it or starts
"Re-run with redesigned plan" (``rerun_with_redesigned_plan``): the redesign is
applied and the case is put back to work through the Resume path
(`run_reopen_services`), so the worker cleans up, restores the carried steps'
state and runs from the first step that is not carried.

Writes lock the project, then the run, then the case, then the draft. Help
code filters plan revisions by ``help_request`` and never sees these rows.

Nothing that `projects.services` imports may import this module (cycle via
`run_reopen_services`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from agents.checkpoint import (
    checkpoint_from_json,
    checkpoint_to_json,
    clear_help_continue_pending,
)
from agents.exceptions import PlanRevisionInvalid
from agents.plan_revision import (
    PlanDraftView,
    PlanStepInput,
    apply_plan_revision,
    build_plan_draft_view,
    plan_fingerprint,
    revision_steps_from_json,
    revision_steps_to_json,
    snapshot_plan,
    snapshots_from_json,
    snapshots_to_json,
)
from agents.plan_revision_limits import validate_case_plan_revision
from agents.types import OrchestratorCheckpoint
from projects.case_result_change_services import (
    is_result_changeable,
    supersede_pending_result_change,
)
from projects.case_review_redesign_store import supersede_case_review_drafts
from projects.exceptions import PlanRevisionError, RunResumeError
from projects.models import (
    PLAN_REVISION_REASON_MAX_LENGTH,
    CasePlanRevision,
    CaseRestartKind,
    PlanRevisionSource,
    PlanRevisionStatus,
    TestRunCaseResume,
    TestRunTestCase,
)
from projects.run_help_mode_policy import resolve_help_mode
from projects.run_reopen_services import (
    REOPENABLE_RUN_STATUSES,
    RESTART_RESET_FIELDS,
    is_valid_help_mode,
    list_reopen_blockers,
    lock_project_and_run,
    reopen_run,
    reset_case_for_restart,
)
from test_sessions.case_activity import CaseActivity
from test_sessions.services.record_case_activity import record_case_activity

_NOT_CHANGEABLE = (
    "Only a Passed, Failed or Blocked case can be redesigned and re-run; this "
    "case is not finished (it may be running again)."
)
_NO_PLAN = "There is no test plan to redesign; suggest a plain Re-run."
_CASE_GONE = "This case no longer exists."
_DRAFT_GONE = "This redesigned plan no longer exists."
_DRAFT_NOT_PENDING = "This redesigned plan is no longer pending. Reload the page."
_CASE_CHANGED = (
    "The case changed since this plan was proposed: it was re-run or resumed. "
    "Ask the agent again."
)
_NOT_FINISHED = "Only a finished or cancelled run can re-run a case."
_INVALID_HELP_MODE = "Choose what happens when a case needs help."


@dataclass(frozen=True)
class ReviewDraftView:
    """A review redesign card's display data; every text is shown escaped."""

    revision_id: int
    # ``PlanRevisionStatus`` value.
    status: str
    # Model-written.
    reason: str
    # First new step that is not carried.
    resume_step: int
    # Side by side: the current plan with statuses, the new plan.
    draft: PlanDraftView
    # Pending, but the case changed since it was proposed (re-run refuses it).
    outdated: bool
    # Server-built from counts only: what "Re-run with redesigned plan" does.
    confirm_body: str


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


def get_pending_review_redesign(pivot: TestRunTestCase) -> CasePlanRevision | None:
    """The case's pending review redesign (one query), or None."""
    return CasePlanRevision.objects.filter(
        test_run_test_case_id=pivot.pk,
        source=PlanRevisionSource.REVIEW,
        status=PlanRevisionStatus.PROPOSED,
    ).first()


def get_review_draft_view(
    revision: CasePlanRevision, pivot: TestRunTestCase
) -> ReviewDraftView:
    """The card of ``revision`` against the case as it is now (no query)."""
    draft = build_plan_draft_view(
        revision_id=revision.pk,
        reason=revision.reason,
        previous_steps=snapshots_from_json(revision.previous_steps),
        steps=revision_steps_from_json(revision.steps),
    )
    outdated = False
    if revision.status == PlanRevisionStatus.PROPOSED:
        checkpoint = _current_checkpoint(pivot)
        outdated = (
            not is_result_changeable(pivot)
            or checkpoint is None
            or plan_fingerprint(checkpoint) != revision.base_fingerprint
        )
    return ReviewDraftView(
        revision_id=revision.pk,
        status=revision.status,
        reason=revision.reason,
        resume_step=revision.resume_step,
        draft=draft,
        outdated=outdated,
        confirm_body=_confirm_body(draft.carried_count, len(draft.new_rows)),
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def propose_review_plan_revision(
    *,
    pivot_id: int,
    steps: Sequence[PlanStepInput],
    reason: str,
    conversation_id: int | None,
) -> CasePlanRevision:
    """Validate a redesign and store it as the case's single pending review draft.

    Raises ``PlanRevisionError`` with a message the agent can act on.
    """
    with transaction.atomic():
        test_run_id = (
            TestRunTestCase.objects.filter(pk=pivot_id)
            .values_list("test_run_id", flat=True)
            .first()
        )
        if test_run_id is None:
            raise PlanRevisionError(_CASE_GONE)
        lock_project_and_run(test_run_id)
        pivot = _lock_pivot(pivot_id)
        if pivot is None:
            raise PlanRevisionError(_CASE_GONE)
        if not is_result_changeable(pivot):
            raise PlanRevisionError(_NOT_CHANGEABLE)
        checkpoint = _current_checkpoint(pivot)
        if checkpoint is None:
            raise PlanRevisionError(_NO_PLAN)
        try:
            validated = validate_case_plan_revision(checkpoint, steps)
        except PlanRevisionInvalid as exc:
            raise PlanRevisionError(exc.message) from exc
        previous_steps = snapshot_plan(checkpoint)
        supersede_case_review_drafts(pivot.pk)
        return CasePlanRevision.objects.create(
            test_run_test_case=pivot,
            help_request=None,
            source=PlanRevisionSource.REVIEW,
            base_fingerprint=plan_fingerprint(checkpoint),
            steps=revision_steps_to_json(validated.steps),
            previous_steps=snapshots_to_json(previous_steps),
            resume_step=validated.resume_step,
            reason=reason.strip()[:PLAN_REVISION_REASON_MAX_LENGTH],
            conversation_id=conversation_id,
        )


def discard_review_plan_revision(
    *, pivot: TestRunTestCase, revision_id: int, user: CustomUser
) -> None:
    """A person rejects the pending review draft; the agent may propose another."""
    updated = CasePlanRevision.objects.filter(
        pk=revision_id,
        test_run_test_case_id=pivot.pk,
        source=PlanRevisionSource.REVIEW,
        status=PlanRevisionStatus.PROPOSED,
    ).update(
        status=PlanRevisionStatus.DISCARDED,
        decided_by=user,
        decided_at=timezone.now(),
    )
    if not updated:
        raise PlanRevisionError(_DRAFT_NOT_PENDING)


def rerun_with_redesigned_plan(
    *,
    pivot: TestRunTestCase,
    revision_id: int,
    user: CustomUser,
    help_mode: str | None,
) -> None:
    """Apply the pending review redesign and re-run the case on its run.

    Resume semantics: the log and screenshots are kept, the Checked/Sent marks
    cleared, an Earlier attempts row (``REDESIGN``) records the replaced
    outcome, and `reopen_run` queues the case after commit. The worker cleans
    up, restores the carried steps' state (none when nothing is carried) and
    runs from the first step that is not carried. ``help_mode`` (a
    ``RunHelpMode`` value) changes the run's mode; ``None`` keeps it.

    Raises ``RunResumeError`` (run status and reopen gates) or
    ``PlanRevisionError`` (the draft); both messages are user-safe.
    ``HelpModeNotAllowedError`` when a non-superuser asks for another mode.
    """
    if help_mode is not None and not is_valid_help_mode(help_mode):
        raise RunResumeError(_INVALID_HELP_MODE)
    with transaction.atomic():
        project, test_run = lock_project_and_run(pivot.test_run_id)
        resolved_help_mode = resolve_help_mode(
            user=user, requested=help_mode, current=test_run.help_mode
        )
        if test_run.status not in REOPENABLE_RUN_STATUSES:
            raise RunResumeError(_NOT_FINISHED)
        blockers = list_reopen_blockers(test_run, project)
        if blockers:
            raise RunResumeError(blockers[0])
        locked = _lock_pivot(pivot.pk)
        if locked is None:
            raise RunResumeError(_CASE_GONE)
        if not is_result_changeable(locked):
            raise PlanRevisionError(_NOT_CHANGEABLE)
        revision = (
            CasePlanRevision.objects.select_for_update()
            .filter(
                pk=revision_id,
                test_run_test_case_id=locked.pk,
                source=PlanRevisionSource.REVIEW,
            )
            .first()
        )
        if revision is None:
            raise PlanRevisionError(_DRAFT_GONE)
        if revision.status != PlanRevisionStatus.PROPOSED:
            raise PlanRevisionError(_DRAFT_NOT_PENDING)
        checkpoint = _current_checkpoint(locked)
        if (
            checkpoint is None
            or plan_fingerprint(checkpoint) != revision.base_fingerprint
        ):
            raise PlanRevisionError(_CASE_CHANGED)
        try:
            validated = validate_case_plan_revision(
                checkpoint,
                [
                    PlanStepInput(
                        description=step.description,
                        expected_result=step.expected_result,
                        raw_timeout=step.timeout_seconds,
                        carried_from=step.carried_from,
                        case_steps=step.case_steps,
                    )
                    for step in revision_steps_from_json(revision.steps)
                ],
            )
        except PlanRevisionInvalid as exc:
            raise PlanRevisionError(
                f"The redesigned plan is no longer valid: {exc.message}"
            ) from exc
        revised = apply_plan_revision(
            checkpoint,
            validated,
            revision_id=revision.pk,
            help_request_id=None,
            reason=revision.reason,
            source="review",
        )
        # ``help`` is already None after a review redesign; kept as a guard so
        # the restart never skips cleanup and restore.
        locked.execution_checkpoint = clear_help_continue_pending(
            checkpoint_to_json(revised)
        )

        now = timezone.now()
        TestRunCaseResume.objects.create(
            test_run_test_case=locked,
            resumed_by=user,
            kind=CaseRestartKind.REDESIGN,
            from_step=validated.resume_step,
            previous_status=locked.status,
            previous_blocked_reason=locked.blocked_reason,
            previous_result=locked.result,
            previous_finished_at=locked.finished_at,
        )
        record_case_activity(
            user=user, pivot_ids=[locked.pk], activity=CaseActivity.REDESIGN_RERUN
        )
        reset_case_for_restart(locked, now)
        # Logs and screenshots are kept.
        locked.save(update_fields=[*RESTART_RESET_FIELDS, "execution_checkpoint"])

        revision.status = PlanRevisionStatus.APPLIED
        revision.decided_by = user
        revision.decided_at = now
        revision.save(update_fields=["status", "decided_by", "decided_at"])
        supersede_pending_result_change(locked.pk)

        reopen_run(test_run, [locked], help_mode=resolved_help_mode)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lock_pivot(pivot_id: int) -> TestRunTestCase | None:
    # Logs can be large and are never touched here.
    return (
        TestRunTestCase.objects.select_for_update()
        .defer("logs")
        .filter(pk=pivot_id)
        .first()
    )


def _current_checkpoint(pivot: TestRunTestCase) -> OrchestratorCheckpoint | None:
    checkpoint = checkpoint_from_json(pivot.execution_checkpoint)
    if checkpoint is None or not checkpoint.plan:
        return None
    return checkpoint


def _confirm_body(carried: int, total: int) -> str:
    """Server-built dialog text from counts only (no user or model text)."""
    runs = _step_range(carried + 1, total)
    if carried:
        restore = f"restores the state of {_step_range(1, carried)}, then runs {runs}"
    else:
        restore = f"then runs {runs}"
    return (
        f"Reopens the run. Cleans up the machine, {restore} of the new plan. "
        "Checked and Sent marks on this case are cleared."
    )


def _step_range(first: int, last: int) -> str:
    if first >= last:
        return f"step {first}"
    return f"steps {first}–{last}"
