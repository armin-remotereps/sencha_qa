"""Mark a run's cases as Checked (a human reviewed the AI verdict), or undo it.

A case can be checked once it has a verdict (Passed, Failed, Blocked). It can
be unchecked until its result has been sent to its source TestRail run. A
Manual run case cannot be checked while its screenshots are still uploading
from the Test Runner (its evidence would be frozen without them).
Checking prepares the AI review of the screenshots the case would attach
(``testrail_evidence_services``): a case is only sent once that review is
done, so the check flash says when screenshots are being reviewed.
Unchecking clears the reason a previous send skipped the case, so checking it
again puts it back in line for the next send.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from projects.checked_push_limits import is_checked_push_live
from projects.exceptions import RunCaseCheckError
from projects.manual_current_submission import uploading_manual_case_ids
from projects.models import TestRun, TestRunTestCase, TestRunTestCaseStatus
from projects.testrail_evidence_services import prepare_testrail_evidence

CHECKABLE_STATUSES: tuple[str, ...] = (
    TestRunTestCaseStatus.SUCCESS,
    TestRunTestCaseStatus.FAILED,
    TestRunTestCaseStatus.BLOCKED,
    TestRunTestCaseStatus.NEEDS_RETEST,
)

REVIEWING_SCREENSHOTS_MESSAGE = (
    "Screenshots are being reviewed before they can be sent."
)
MANUAL_UPLOADING_MESSAGE = (
    "Screenshots are still uploading from the Test Runner: check these cases "
    "once they have arrived."
)
_NOTHING_TO_CHECK_MESSAGE = (
    "None of the selected cases could be checked: they have no verdict "
    "yet or are already checked."
)


@dataclass(frozen=True)
class CheckOutcome:
    """How many selected cases changed, how many were skipped, and how many
    checked cases wait for their screenshot review."""

    changed: int
    skipped: int
    reviewing: int = 0


@dataclass(frozen=True)
class LockedCheck:
    """What ``check_locked_cases`` did: cases checked, and how many of them
    went ``pending`` for the screenshot review."""

    changed: int
    reviewing: int


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


@transaction.atomic
def check_run_cases(
    *, test_run: TestRun, pivot_ids: Sequence[int], user: CustomUser
) -> CheckOutcome:
    """Check the selected cases; raises `RunCaseCheckError` if none changed.

    Also prepares the review of which screenshots the next TestRail send
    attaches (``reviewing`` counts the cases queued for the AI review).
    The cases are locked so a concurrent Apply or Resume cannot change a
    result between the selection and the check.
    """
    selected = set(pivot_ids)
    pivots = list(
        test_run.pivot_entries.select_for_update()
        .filter(id__in=selected, status__in=CHECKABLE_STATUSES, checked_at__isnull=True)
        .only("id", "status")
    )
    if not pivots:
        raise RunCaseCheckError(_NOTHING_TO_CHECK_MESSAGE)
    checked = check_locked_cases(pivots, user=user, now=timezone.now())
    if checked.changed == 0:
        # Every eligible case is a manual one still uploading.
        raise RunCaseCheckError(MANUAL_UPLOADING_MESSAGE)
    return CheckOutcome(
        changed=checked.changed,
        skipped=len(selected) - checked.changed,
        reviewing=checked.reviewing,
    )


def check_locked_pivots(
    pivots: Sequence[TestRunTestCase], *, user: CustomUser, now: datetime
) -> int:
    """``check_locked_cases`` returning only the number of cases checked."""
    return check_locked_cases(pivots, user=user, now=now).changed


def check_locked_cases(
    pivots: Sequence[TestRunTestCase], *, user: CustomUser, now: datetime
) -> LockedCheck:
    """Check pivots the caller locked in its open transaction.

    ``pivots`` are unchecked cases with a verdict, loaded with at least ``id``
    and ``status`` under ``select_for_update``. Manual cases whose screenshots
    are still uploading are left unchecked (callers that report them use
    ``uploading_manual_case_ids`` first). Prepares the others' screenshot
    review (query-only; the AI review is queued on commit), then sets
    Checked by ``user`` and clears a previous send's skip reason (one UPDATE).
    """
    if not pivots:
        return LockedCheck(changed=0, reviewing=0)
    uploading = uploading_manual_case_ids(pivot.id for pivot in pivots)
    pivots = [pivot for pivot in pivots if pivot.id not in uploading]
    if not pivots:
        return LockedCheck(changed=0, reviewing=0)
    reviewing = prepare_testrail_evidence(pivots)
    changed = TestRunTestCase.objects.filter(
        id__in=[pivot.id for pivot in pivots]
    ).update(
        checked_at=now,
        checked_by=user,
        testrail_source_skip_reason="",
        updated_at=now,
    )
    return LockedCheck(changed=changed, reviewing=reviewing)


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
    """User message such as "Checked 4 cases · 1 skipped", plus
    ``REVIEWING_SCREENSHOTS_MESSAGE`` when a checked case awaits its review."""
    noun = "case" if outcome.changed == 1 else "cases"
    message = f"{verb} {outcome.changed} {noun}"
    if outcome.skipped:
        message += f" · {outcome.skipped} skipped"
    if outcome.reviewing:
        message += f". {REVIEWING_SCREENSHOTS_MESSAGE}"
    return message
