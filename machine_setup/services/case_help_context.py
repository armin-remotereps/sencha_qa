"""Load a help request's brief data: shared by the auto-fix, the help chat and its tools.

One query for the request with its case and run; the person brief and the
case update (``get_help_case``) add one for the case's help history and one
for the pending redesign draft.
"""

from __future__ import annotations

import dataclasses

from agents.checkpoint import checkpoint_from_json
from agents.plan_revision import OldStepStatus, carriable_steps, snapshot_plan
from agents.types import OrchestratorCheckpoint
from machine_setup.case_help_brief import (
    ACTIONS_DIGEST_MAX,
    BriefAudience,
    CaseHelpBriefData,
    EarlierRevision,
    HelpHistoryEntry,
    PlanStep,
    build_case_help_brief,
    build_case_help_update,
)
from projects.case_plan_revision_services import get_pending_plan_draft
from projects.case_step_refs import case_steps_label
from projects.models import (
    CaseHelpRequest,
    HelpContinueMode,
    HelpResolution,
    TestRunStatus,
    TestRunTestCaseStatus,
)

# Newest help requests of the case listed in the person brief.
_HISTORY_LIMIT = 6

_STATUS_LABELS: dict[OldStepStatus, str] = {
    "passed": "passed",
    "failed": "failed",
    "not_run": "not run",
    "needs_help": "needs help",
}


def load_help_request(help_request_id: int) -> CaseHelpRequest | None:
    """The request with its case and run loaded; one query."""
    return (
        CaseHelpRequest.objects.select_related(
            "test_run_test_case__test_case", "test_run_test_case__test_run"
        )
        .defer("test_run_test_case__logs")
        .filter(pk=help_request_id)
        .first()
    )


def help_request_is_waiting(request: CaseHelpRequest) -> bool:
    """Open request, PAUSED run, case in Need help (from the loaded rows)."""
    pivot = request.test_run_test_case
    return (
        request.is_open
        and pivot.test_run.status == TestRunStatus.PAUSED
        and pivot.status == TestRunTestCaseStatus.NEED_HELP
    )


def build_help_brief(request: CaseHelpRequest, *, audience: BriefAudience) -> str:
    """The brief of ``request`` (loaded by ``load_help_request``)."""
    return build_case_help_brief(
        case_help_brief_data(request, audience=audience), audience=audience
    )


def build_help_case_update(request: CaseHelpRequest) -> str:
    """The ``get_help_case`` result of ``request`` (loaded by ``load_help_request``).

    Same queries as the person brief, without the case text.
    """
    return build_case_help_update(case_help_brief_data(request, audience="person"))


def case_help_brief_data(
    request: CaseHelpRequest, *, audience: BriefAudience
) -> CaseHelpBriefData:
    pivot = request.test_run_test_case
    case = pivot.test_case
    checkpoint = checkpoint_from_json(pivot.execution_checkpoint)
    base = CaseHelpBriefData(
        run_name=pivot.test_run.name,
        case_title=case.title,
        preconditions=case.preconditions,
        steps=case.steps,
        expected=case.expected,
        plan=(
            tuple(
                PlanStep(
                    description=task.description,
                    expected_result=task.expected_result,
                    case_steps_label=case_steps_label(
                        task.case_steps, testrail=checkpoint.case_steps_testrail
                    ),
                )
                for task in checkpoint.plan
            )
            if checkpoint is not None
            else ()
        ),
        step_num=request.step_num,
        reason=request.reason,
        ask=request.ask,
        actions_digest=failed_attempt_digest(checkpoint, request.step_num),
        fault_label=request.get_fault_display() if request.fault else "",
        continue_mode=HelpContinueMode(request.continue_mode),
    )
    if audience == "auto_fix":
        return base
    waiting = help_request_is_waiting(request)
    return dataclasses.replace(
        base,
        plan=_person_plan(checkpoint),
        request_open=waiting,
        status_text=_status_text(request, waiting),
        help_history=_help_history(request),
        revisions=_revisions(checkpoint),
        pending_draft=get_pending_plan_draft(request.pk) if waiting else None,
    )


def failed_attempt_digest(
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


def describe_help_status(help_request_id: int) -> str:
    """One line for the help chat's system prompt; one query."""
    request = (
        CaseHelpRequest.objects.select_related("test_run_test_case__test_run")
        .only(
            "id",
            "step_num",
            "resolution",
            "test_run_test_case__id",
            "test_run_test_case__status",
            "test_run_test_case__test_run__id",
            "test_run_test_case__test_run__status",
        )
        .filter(pk=help_request_id)
        .first()
    )
    if request is None:
        return "The help request of this chat no longer exists."
    return _status_text(request, help_request_is_waiting(request))


def _status_text(request: CaseHelpRequest, waiting: bool) -> str:
    if waiting:
        return (
            f"The case is waiting for help at step {request.step_num}; the run "
            "stays paused until the person resolves it."
        )
    if request.resolution != HelpResolution.OPEN:
        return (
            "The case is no longer waiting for help: it was resolved "
            f"({request.get_resolution_display()}). The help tools refuse; "
            "this chat continues as an ordinary machine setup chat."
        )
    return (
        "The case is no longer waiting for help (the run moved on). The help "
        "tools refuse; this chat continues as an ordinary machine setup chat."
    )


def _person_plan(checkpoint: OrchestratorCheckpoint | None) -> tuple[PlanStep, ...]:
    if checkpoint is None:
        return ()
    carriable = carriable_steps(checkpoint)
    return tuple(
        PlanStep(
            description=snapshot.description,
            expected_result=snapshot.expected_result,
            status_label=_STATUS_LABELS[snapshot.status],
            carriable=num in carriable,
            case_steps_label=case_steps_label(
                task.case_steps, testrail=checkpoint.case_steps_testrail
            ),
        )
        # ``snapshot_plan`` has one snapshot per plan step, in order.
        for num, (snapshot, task) in enumerate(
            zip(snapshot_plan(checkpoint), checkpoint.plan, strict=True), start=1
        )
    )


def _help_history(request: CaseHelpRequest) -> tuple[HelpHistoryEntry, ...]:
    rows = list(
        CaseHelpRequest.objects.filter(
            test_run_test_case_id=request.test_run_test_case_id
        )
        .only(
            "id",
            "step_num",
            "reason",
            "ask",
            "resolution",
            "note",
            "auto_fix_summary",
            "requested_at",
        )
        .order_by("-requested_at", "-id")[:_HISTORY_LIMIT]
    )
    rows.reverse()
    return tuple(
        HelpHistoryEntry(
            step_num=row.step_num,
            reason=row.reason,
            ask=row.ask,
            resolution_label=row.get_resolution_display(),
            note=row.note,
            auto_fix_summary=row.auto_fix_summary,
            current=row.pk == request.pk,
        )
        for row in rows
    )


def _revisions(
    checkpoint: OrchestratorCheckpoint | None,
) -> tuple[EarlierRevision, ...]:
    if checkpoint is None:
        return ()
    return tuple(
        EarlierRevision(
            version=record.version,
            reason=record.reason,
            previous_steps=tuple(
                description for description, _ in record.previous_plan
            ),
            mapping=record.mapping,
            paused_step=record.paused_step,
            resume_step=record.resume_step,
        )
        for record in checkpoint.plan_revisions
    )
