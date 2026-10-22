"""Mark a run's cases as Checked (a human reviewed the AI verdict), or undo it.

A case can be checked once it has a verdict (Passed, Failed, Blocked). It can
be unchecked until its result has been sent to its source TestRail run.
Unchecking clears the reason a previous send skipped the case, so checking it
again puts it back in line for the next send.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from projects.checked_push_limits import is_checked_push_live
from projects.exceptions import RunCaseCheckError
from projects.models import TestRun, TestRunTestCase, TestRunTestCaseStatus

CHECKABLE_STATUSES: tuple[str, ...] = (
    TestRunTestCaseStatus.SUCCESS,
    TestRunTestCaseStatus.FAILED,
    TestRunTestCaseStatus.BLOCKED,
)


@dataclass(frozen=True)
class CheckOutcome:
    """How many selected cases changed, and how many were skipped."""

    changed: int
    skipped: int


def is_checkable(pivot: TestRunTestCase) -> bool:
    """Whether an already-loaded pivot can be checked now (no query)."""
    return pivot.status in CHECKABLE_STATUSES and pivot.checked_at is None


def is_uncheckable(pivot: TestRunTestCase) -> bool:
    """Whether an already-loaded pivot is checked and not yet sent (no query)."""
    return pivot.checked_at is not None and pivot.testrail_source_result_id is None


def list_checkable_pivot_ids(test_run: TestRun) -> list[int]:
    """Pivot ids of `test_run` that can be checked now."""
    return list(
        test_run.pivot_entries.filter(
            status__in=CHECKABLE_STATUSES, checked_at__isnull=True
        ).values_list("id", flat=True)
    )


def list_uncheckable_pivot_ids(test_run: TestRun) -> list[int]:
    """Pivot ids of `test_run` that are checked and not yet sent."""
    return list(
        test_run.pivot_entries.filter(
            checked_at__isnull=False, testrail_source_result_id__isnull=True
        ).values_list("id", flat=True)
    )


def check_run_cases(
    *, test_run: TestRun, pivot_ids: Sequence[int], user: CustomUser
) -> CheckOutcome:
    """Check the selected cases; raises `RunCaseCheckError` if none changed."""
    selected = set(pivot_ids)
    now = timezone.now()
    changed = test_run.pivot_entries.filter(
        id__in=selected, status__in=CHECKABLE_STATUSES, checked_at__isnull=True
    ).update(
        checked_at=now,
        checked_by=user,
        testrail_source_skip_reason="",
        updated_at=now,
    )
    if changed == 0:
        raise RunCaseCheckError(
            "None of the selected cases could be checked: they have no verdict "
            "yet or are already checked."
        )
    return CheckOutcome(changed=changed, skipped=len(selected) - changed)


@transaction.atomic
def uncheck_run_cases(*, test_run: TestRun, pivot_ids: Sequence[int]) -> CheckOutcome:
    """Uncheck the selected unsent cases; raises `RunCaseCheckError` if none changed.

    Refused while a send is live: the send planned its cases when it started and
    would still post an unchecked case. The run row is locked so a send cannot
    take the push slot between this check and the update.
    """
    locked = (
        TestRun.objects.select_for_update()
        .only("id", "testrail_source_push_status", "testrail_source_push_started_at")
        .get(pk=test_run.pk)
    )
    if is_checked_push_live(locked, timezone.now()):
        raise RunCaseCheckError(
            "Wait for the checked results to finish sending to TestRail."
        )
    selected = set(pivot_ids)
    changed = test_run.pivot_entries.filter(
        id__in=selected,
        checked_at__isnull=False,
        testrail_source_result_id__isnull=True,
    ).update(
        checked_at=None,
        checked_by=None,
        testrail_source_skip_reason="",
        updated_at=timezone.now(),
    )
    if changed == 0:
        raise RunCaseCheckError(
            "None of the selected cases could be unchecked: they are not checked "
            "or were already sent to TestRail."
        )
    return CheckOutcome(changed=changed, skipped=len(selected) - changed)


def format_check_message(outcome: CheckOutcome, *, verb: str) -> str:
    """User message such as "Checked 4 cases · 1 skipped"."""
    noun = "case" if outcome.changed == 1 else "cases"
    message = f"{verb} {outcome.changed} {noun}"
    if outcome.skipped:
        message += f" · {outcome.skipped} skipped"
    return message
