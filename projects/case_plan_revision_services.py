"""Use cases for a help-chat redesign of a case's test plan (a draft until applied).

The help-chat agent proposes a full new step list (``propose_plan_revision``);
it is validated and stored as the help request's single pending draft. A person
discards it (``discard_plan_revision``) or applies it with Continue, which calls
``apply_plan_revision_locked`` under its own locks. Mark failed, Skip and a
Continue without the draft supersede it (``supersede_pending_revisions``).

Every write locks the project, then the run, then the help request, then the
case (the order every help writer uses) and requires the run PAUSED, the request
open and the case Need help. Abort supersedes the drafts of the request it
cancels; every read here still filters on an open request. A superseded draft
is never displayed, so its ``steps`` / ``previous_steps`` are cleared.
"""

from __future__ import annotations

from collections.abc import Sequence

from django.db import transaction
from django.db.models import Prefetch, QuerySet
from django.utils import timezone

from accounts.models import CustomUser
from agents.checkpoint import checkpoint_from_json, checkpoint_to_json
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
from projects.exceptions import PlanRevisionError, RunHelpError
from projects.models import (
    PLAN_REVISION_REASON_MAX_LENGTH,
    CaseHelpRequest,
    CasePlanRevision,
    HelpResolution,
    PlanRevisionStatus,
    TestRun,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.run_reopen_services import lock_project_and_run

NOT_WAITING_FOR_HELP = "This case is no longer waiting for help."
_NO_PLAN = "There is no test plan to redesign yet."
_DRAFT_NOT_PENDING = "This draft is no longer pending. Reload the page."
_NEWER_DRAFT = "The agent proposed a newer plan. Review it, then continue again."
_DRAFT_GONE = (
    "The redesigned plan is no longer pending. Review the case, then continue again."
)
_CASE_CHANGED = "The case changed since this plan was proposed."

# Prefetched onto a help request: its applied and discarded drafts, oldest first.
DECIDED_REVISIONS_ATTR = "decided_plan_revisions"


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


def get_pending_plan_draft(help_request_id: int) -> PlanDraftView | None:
    """The open request's pending draft as a side-by-side view (one query)."""
    revision = (
        CasePlanRevision.objects.filter(
            help_request_id=help_request_id,
            status=PlanRevisionStatus.PROPOSED,
            help_request__resolution=HelpResolution.OPEN,
        )
        .only("id", "reason", "steps", "previous_steps")
        .first()
    )
    if revision is None:
        return None
    return build_plan_draft_view(
        revision_id=revision.pk,
        reason=revision.reason,
        previous_steps=snapshots_from_json(revision.previous_steps),
        steps=revision_steps_from_json(revision.steps),
    )


def with_decided_revisions() -> Prefetch[str, QuerySet[CasePlanRevision], str]:
    """Prefetch a help request's applied and discarded drafts (help history)."""
    return Prefetch(
        "plan_revisions",
        queryset=CasePlanRevision.objects.filter(
            status__in=(PlanRevisionStatus.APPLIED, PlanRevisionStatus.DISCARDED)
        )
        .select_related("decided_by")
        .only(
            "id",
            "help_request_id",
            "steps",
            "resume_step",
            "reason",
            "status",
            "proposed_at",
            "decided_at",
            "decided_by__first_name",
            "decided_by__last_name",
            "decided_by__email",
        )
        .order_by("proposed_at", "id"),
        to_attr=DECIDED_REVISIONS_ATTR,
    )


def plan_revision_belongs_to(*, pivot: TestRunTestCase, revision_id: int) -> bool:
    """Whether the draft is one of this run case's (for the view's 404)."""
    return CasePlanRevision.objects.filter(
        pk=revision_id, test_run_test_case_id=pivot.pk
    ).exists()


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def propose_plan_revision(
    *,
    help_request_id: int,
    steps: Sequence[PlanStepInput],
    reason: str,
    conversation_id: int | None,
) -> PlanDraftView:
    """Validate a redesign and store it as the request's single pending draft.

    Raises ``PlanRevisionError`` with a message the agent can act on.
    """
    with transaction.atomic():
        try:
            _, pivot, request = lock_waiting_help_request(help_request_id)
        except RunHelpError as exc:
            raise PlanRevisionError(exc.message) from exc
        checkpoint = _current_checkpoint(pivot)
        if checkpoint is None:
            raise PlanRevisionError(_NO_PLAN)
        try:
            validated = validate_case_plan_revision(checkpoint, steps)
        except PlanRevisionInvalid as exc:
            raise PlanRevisionError(exc.message) from exc
        previous_steps = snapshot_plan(checkpoint)
        reason = reason.strip()[:PLAN_REVISION_REASON_MAX_LENGTH]
        supersede_pending_revisions(request)
        revision = CasePlanRevision.objects.create(
            test_run_test_case=pivot,
            help_request=request,
            base_fingerprint=plan_fingerprint(checkpoint),
            steps=revision_steps_to_json(validated.steps),
            previous_steps=snapshots_to_json(previous_steps),
            resume_step=validated.resume_step,
            reason=reason,
            conversation_id=conversation_id,
        )
    return build_plan_draft_view(
        revision_id=revision.pk,
        reason=reason,
        previous_steps=previous_steps,
        steps=validated.steps,
    )


def discard_plan_revision(
    *, pivot: TestRunTestCase, revision_id: int, user: CustomUser
) -> None:
    """A person rejects the pending draft; the agent may propose another."""
    with transaction.atomic():
        _, test_run = lock_project_and_run(pivot.test_run_id)
        if test_run.status != TestRunStatus.PAUSED:
            raise PlanRevisionError(NOT_WAITING_FOR_HELP)
        request = (
            CaseHelpRequest.objects.select_for_update()
            .filter(test_run_test_case_id=pivot.pk, resolution=HelpResolution.OPEN)
            .first()
        )
        if request is None:
            raise PlanRevisionError(NOT_WAITING_FOR_HELP)
        updated = CasePlanRevision.objects.filter(
            pk=revision_id,
            help_request_id=request.pk,
            status=PlanRevisionStatus.PROPOSED,
        ).update(
            status=PlanRevisionStatus.DISCARDED,
            decided_by=user,
            decided_at=timezone.now(),
        )
        if not updated:
            raise PlanRevisionError(_DRAFT_NOT_PENDING)


# ---------------------------------------------------------------------------
# Internal: called by case_help_services / case_help_proposal_services under
# their locks (project -> run -> request -> case).
# ---------------------------------------------------------------------------


def lock_waiting_help_request(
    help_request_id: int,
) -> tuple[TestRun, TestRunTestCase, CaseHelpRequest]:
    """Lock project, run, request and case of a request that waits for help.

    Must run inside ``transaction.atomic()``. Raises ``RunHelpError`` (not
    waiting for help) unless the run is PAUSED, the request open and the case
    Need help.
    """
    test_run_id = (
        CaseHelpRequest.objects.filter(pk=help_request_id)
        .values_list("test_run_test_case__test_run_id", flat=True)
        .first()
    )
    if test_run_id is None:
        raise RunHelpError(NOT_WAITING_FOR_HELP)
    _, test_run = lock_project_and_run(test_run_id)
    if test_run.status != TestRunStatus.PAUSED:
        raise RunHelpError(NOT_WAITING_FOR_HELP)
    request = (
        CaseHelpRequest.objects.select_for_update()
        .filter(pk=help_request_id, resolution=HelpResolution.OPEN)
        .first()
    )
    if request is None:
        raise RunHelpError(NOT_WAITING_FOR_HELP)
    pivot = (
        TestRunTestCase.objects.select_for_update()
        .defer("logs")
        .filter(pk=request.test_run_test_case_id)
        .first()
    )
    if pivot is None or pivot.status != TestRunTestCaseStatus.NEED_HELP:
        raise RunHelpError(NOT_WAITING_FOR_HELP)
    request.test_run_test_case = pivot
    return test_run, pivot, request


def pending_revision_id_locked(request: CaseHelpRequest) -> int | None:
    """The id of the request's pending draft, row-locked; None when none."""
    return (
        CasePlanRevision.objects.select_for_update()
        .filter(help_request_id=request.pk, status=PlanRevisionStatus.PROPOSED)
        .values_list("id", flat=True)
        .first()
    )


def apply_plan_revision_locked(
    *,
    pivot: TestRunTestCase,
    request: CaseHelpRequest,
    revision_id: int,
    user: CustomUser,
) -> None:
    """Apply the pending draft the person saw to the locked case's checkpoint.

    The caller holds the project, run, request and case locks. Only assigns
    ``pivot.execution_checkpoint`` (not saved): the caller then writes the help
    key onto that in-memory checkpoint and saves the case once. Raises
    ``RunHelpError`` when the draft is not the pending one, the case changed
    since it was proposed, or it no longer validates.
    """
    revision = (
        CasePlanRevision.objects.select_for_update()
        .filter(help_request_id=request.pk, status=PlanRevisionStatus.PROPOSED)
        .first()
    )
    if revision is None:
        raise RunHelpError(_DRAFT_GONE)
    if revision.pk != revision_id:
        raise RunHelpError(_NEWER_DRAFT)
    checkpoint = _current_checkpoint(pivot)
    if checkpoint is None or plan_fingerprint(checkpoint) != revision.base_fingerprint:
        raise RunHelpError(_CASE_CHANGED)
    stored = revision_steps_from_json(revision.steps)
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
                for step in stored
            ],
        )
    except PlanRevisionInvalid as exc:
        raise RunHelpError(
            f"The redesigned plan is no longer valid: {exc.message}"
        ) from exc
    revised = apply_plan_revision(
        checkpoint,
        validated,
        revision_id=revision.pk,
        help_request_id=request.pk,
        reason=revision.reason,
    )
    pivot.execution_checkpoint = checkpoint_to_json(revised)
    revision.status = PlanRevisionStatus.APPLIED
    revision.decided_by = user
    revision.decided_at = timezone.now()
    revision.save(update_fields=["status", "decided_by", "decided_at"])


def supersede_pending_revisions(request: CaseHelpRequest) -> None:
    """Retire the request's pending draft (a newer one, or resolved without it).

    A superseded draft is never displayed: its step lists are cleared.
    """
    CasePlanRevision.objects.filter(
        help_request_id=request.pk, status=PlanRevisionStatus.PROPOSED
    ).update(
        status=PlanRevisionStatus.SUPERSEDED,
        decided_at=timezone.now(),
        steps=[],
        previous_steps=[],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _current_checkpoint(pivot: TestRunTestCase) -> OrchestratorCheckpoint | None:
    checkpoint = checkpoint_from_json(pivot.execution_checkpoint)
    if checkpoint is None or not checkpoint.plan:
        return None
    return checkpoint
