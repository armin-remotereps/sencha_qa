"""What the run case page shows about the case's result chat and result changes."""

from __future__ import annotations

from dataclasses import dataclass

from machine_setup.models import SetupConversation, SetupConversationKind
from projects.case_result_change_services import (
    is_result_changeable,
    list_result_changes,
)
from projects.case_result_text import result_status_label
from projects.models import (
    CasePlanRevision,
    CaseResultChange,
    PlanRevisionSource,
    PlanRevisionStatus,
    TestRun,
    TestRunStatus,
    TestRunTestCase,
)

_RUN_STARTED = (
    "A test run of this project is in progress. You can discuss this result "
    "when it finishes."
)


@dataclass(frozen=True)
class CaseResultPanel:
    # The case has a Passed / Failed / Blocked result a person may discuss.
    can_discuss_result: bool
    # The case's non-archived result chat thread; None when none was opened.
    result_thread_id: int | None
    # Why the result chat cannot take a message now ("" when it can).
    result_chat_blocker: str
    # Applied result changes, newest first, ``decided_by`` loaded.
    result_changes: list[CaseResultChange]
    # The applied change that wrote the current result; None when the agent's
    # (or a help action's) verdict is the current one.
    current_result_change: CaseResultChange | None
    # The result the current change replaced ("Passed" / "Failed" / ...);
    # "" without a current change.
    current_result_from_label: str
    # The result-chat agent proposed a redesigned re-run that waits for a person.
    pending_review_redesign: bool


def get_case_result_panel(pivot: TestRunTestCase) -> CaseResultPanel:
    """Result chat state of the case (``pivot.test_run`` loaded; 2 to 4 queries)."""
    result_changes = list_result_changes(pivot)
    result_thread_id = (
        SetupConversation.objects.filter(
            result_case_id=pivot.pk,
            kind=SetupConversationKind.CASE_RESULT,
            archived_at__isnull=True,
        )
        .values_list("id", flat=True)
        .first()
    )
    can_discuss = is_result_changeable(pivot)
    blocker = ""
    pending_redesign = False
    if can_discuss:
        if TestRun.objects.filter(
            project_id=pivot.test_run.project_id, status=TestRunStatus.STARTED
        ).exists():
            blocker = _RUN_STARTED
        pending_redesign = CasePlanRevision.objects.filter(
            test_run_test_case_id=pivot.pk,
            source=PlanRevisionSource.REVIEW,
            status=PlanRevisionStatus.PROPOSED,
        ).exists()
    current_change = _current_change(pivot, result_changes)
    return CaseResultPanel(
        can_discuss_result=can_discuss,
        result_thread_id=result_thread_id,
        result_chat_blocker=blocker,
        result_changes=result_changes,
        current_result_change=current_change,
        current_result_from_label=(
            result_status_label(current_change.from_status)
            if current_change is not None
            else ""
        ),
        pending_review_redesign=pending_redesign,
    )


def _current_change(
    pivot: TestRunTestCase, result_changes: list[CaseResultChange]
) -> CaseResultChange | None:
    """The newest applied change, while the case still holds the result it wrote.

    Apply keeps ``finished_at``; a re-run or resume finishes the case again
    later, so a change decided before the latest finish no longer applies.
    """
    if not result_changes or not is_result_changeable(pivot):
        return None
    latest = result_changes[0]
    if latest.to_status != pivot.status or latest.decided_at is None:
        return None
    if pivot.finished_at is not None and latest.decided_at < pivot.finished_at:
        return None
    return latest
