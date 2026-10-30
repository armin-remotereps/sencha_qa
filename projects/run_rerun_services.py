"""Re-run selected cases of a finished run in place, from step 1.

Unlike Resume, a re-run starts the case over: its checkpoint (and so its step
progress) and screenshots are dropped, so the next "Send checked" attaches
only the new attempt's evidence. The log is cleared too; only the replaced
outcome is kept, in the case's history. Cases run in the
run's order (`RUN_CASE_ORDERING`) restricted to the selection.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from projects.case_credit import credit_cases
from projects.case_retest_marking import clear_retest_mark
from projects.exceptions import RunRerunError
from projects.models import (
    RETEST_CLEARED,
    CaseRestartKind,
    Project,
    TestRun,
    TestRunCaseResume,
    TestRunScreenshot,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.run_case_order import RUN_CASE_ORDERING
from projects.run_help_mode_policy import resolve_help_mode
from projects.run_reopen_services import (
    REOPENABLE_RUN_STATUSES,
    is_valid_help_mode,
    list_reopen_blockers,
    lock_project_and_run,
    reopen_run,
)
from test_sessions.case_activity import CaseActivity
from test_sessions.services.record_case_activity import record_case_activity

logger = logging.getLogger(__name__)

_NOT_FINISHED = "Only a finished or cancelled run can be re-run."
_NOTHING_SELECTED = "None of the selected cases belong to this run."
_INVALID_HELP_MODE = "Choose what happens when a case needs help."


@dataclass(frozen=True)
class RerunPanel:
    """What the run page shows for "Re-run"; blocker texts are user-safe."""

    can_rerun: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class RerunOutcome:
    """How many selected cases were queued, and how many were skipped."""

    queued: int
    skipped: int


def is_rerun_open(test_run: TestRun) -> bool:
    """Whether the run's cases can be selected for a re-run (no query)."""
    return test_run.status in REOPENABLE_RUN_STATUSES


def rerunnable_ids_on_page(
    test_run: TestRun, pivots: Iterable[TestRunTestCase]
) -> list[int]:
    """The page's pivot ids a re-run may select: all of them on a finished run."""
    if not is_rerun_open(test_run):
        return []
    return [pivot.pk for pivot in pivots]


def list_rerunnable_pivot_ids(test_run: TestRun) -> list[int]:
    """Every pivot id of a finished run (any status), else none (1 query)."""
    if not is_rerun_open(test_run):
        return []
    return list(test_run.pivot_entries.values_list("id", flat=True))


def get_rerun_panel(test_run: TestRun, project: Project) -> RerunPanel:
    """Gates for the Re-run button; hidden (no queries) unless the run finished."""
    if not is_rerun_open(test_run):
        return RerunPanel(can_rerun=False, blockers=())
    blockers = list_reopen_blockers(test_run, project)
    return RerunPanel(can_rerun=not blockers, blockers=tuple(blockers))


def rerun_test_run_cases(
    *,
    test_run: TestRun,
    pivot_ids: Sequence[int],
    user: CustomUser,
    help_mode: str | None = None,
) -> RerunOutcome:
    """Re-run the selected cases of the run from step 1, in run order.

    ``help_mode`` (a ``RunHelpMode`` value) changes the run's mode; ``None``
    keeps it. Only a superuser may change it (``HelpModeNotAllowedError``).
    """
    if help_mode is not None and not is_valid_help_mode(help_mode):
        raise RunRerunError(_INVALID_HELP_MODE)
    with transaction.atomic():
        project, locked_run = lock_project_and_run(test_run.pk)
        resolved_help_mode = resolve_help_mode(
            user=user, requested=help_mode, current=locked_run.help_mode
        )
        if not is_rerun_open(locked_run):
            raise RunRerunError(_NOT_FINISHED)
        blockers = list_reopen_blockers(locked_run, project)
        if blockers:
            raise RunRerunError(blockers[0])

        # Filtering on the run keeps foreign ids out; logs are never loaded.
        pivots = list(
            TestRunTestCase.objects.filter(test_run_id=locked_run.pk, pk__in=pivot_ids)
            .defer("logs")
            .order_by(*RUN_CASE_ORDERING)
        )
        if not pivots:
            raise RunRerunError(_NOTHING_SELECTED)
        selected_ids = [pivot.pk for pivot in pivots]
        record_case_activity(
            user=user, pivot_ids=selected_ids, activity=CaseActivity.RERUN
        )
        credit_cases(user=user, pivot_ids=selected_ids)

        TestRunCaseResume.objects.bulk_create(
            [_snapshot(pivot, user) for pivot in pivots]
        )

        screenshots = TestRunScreenshot.objects.filter(
            test_run_test_case_id__in=selected_ids
        )
        file_names = list(screenshots.values_list("image", flat=True))
        screenshots.delete()

        now = timezone.now()
        # Every execution and Checked/Sent field, as `_reset_in_memory` does.
        TestRunTestCase.objects.filter(pk__in=selected_ids).update(
            status=TestRunTestCaseStatus.CREATED,
            blocked_reason="",
            result="",
            started_at=None,
            finished_at=None,
            segment_started_at=None,
            active_duration=None,
            execution_checkpoint={},
            checked_at=None,
            checked_by=None,
            testrail_source_result_id=None,
            testrail_source_sent_at=None,
            testrail_source_attachment_count=0,
            testrail_source_skip_reason="",
            logs="",
            **RETEST_CLEARED,
            # update() does not run auto_now.
            updated_at=now,
        )
        for pivot in pivots:
            _reset_in_memory(pivot, now)

        reopen_run(locked_run, pivots, help_mode=resolved_help_mode)
        # A rolled-back re-run keeps its evidence.
        transaction.on_commit(lambda: _delete_screenshot_files(file_names))
    return RerunOutcome(queued=len(pivots), skipped=len(set(pivot_ids)) - len(pivots))


def format_rerun_message(outcome: RerunOutcome) -> str:
    """Flash text, e.g. "Re-running 3 cases · 1 skipped"."""
    noun = "case" if outcome.queued == 1 else "cases"
    message = f"Re-running {outcome.queued} {noun}"
    if outcome.skipped:
        message += f" · {outcome.skipped} skipped"
    return message


def _snapshot(pivot: TestRunTestCase, user: CustomUser) -> TestRunCaseResume:
    return TestRunCaseResume(
        test_run_test_case=pivot,
        kind=CaseRestartKind.RERUN,
        resumed_by=user,
        from_step=None,
        previous_status=pivot.status,
        previous_blocked_reason=pivot.blocked_reason,
        previous_result=pivot.result,
        previous_finished_at=pivot.finished_at,
    )


def _reset_in_memory(pivot: TestRunTestCase, now: datetime) -> None:
    """Mirror the UPDATE on a loaded pivot, which is broadcast as it is."""
    pivot.status = TestRunTestCaseStatus.CREATED
    pivot.blocked_reason = ""
    pivot.result = ""
    pivot.started_at = None
    pivot.finished_at = None
    pivot.segment_started_at = None
    pivot.active_duration = None
    pivot.execution_checkpoint = {}
    pivot.checked_at = None
    pivot.checked_by = None
    pivot.testrail_source_result_id = None
    pivot.testrail_source_sent_at = None
    pivot.testrail_source_attachment_count = 0
    pivot.testrail_source_skip_reason = ""
    clear_retest_mark(pivot)
    pivot.updated_at = now


def _delete_screenshot_files(file_names: Sequence[str]) -> None:
    image_field = TestRunScreenshot._meta.get_field("image")
    for name in file_names:
        try:
            image_field.storage.delete(name)
        except OSError:
            logger.exception("Could not delete re-run screenshot %s", name)
