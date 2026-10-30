"""Send selected failed cases to their source TestRail run now ("case send").

Unlike "Send checked to TestRail" (finished runs, every checked case), a case
send works while the run is Running or Paused too, and sends exactly the
selected cases. It checks them first (evidence frozen, Checked by the sender;
already checked cases keep their reviewer) and then reuses the run's single
send slot, task, plan and idempotence from `testrail_checked_push_services`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from projects.exceptions import NothingToSendError, RunNotFinishedError
from projects.models import (
    TestRun,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.run_case_check_services import check_locked_pivots
from projects.testrail_checked_push_services import (
    TESTRAIL_NOT_CONFIGURED_MESSAGE,
    dispatch_checked_push,
    fail_stale_checked_push,
    take_checked_push_slot,
)
from projects.testrail_services import TestRailNotConfiguredError

# Verdicts that may be sent before the run finishes. A Failed case is final
# for its execution, so running / Need help / Your turn cases never qualify.
CASE_SEND_STATUSES: tuple[str, ...] = (TestRunTestCaseStatus.FAILED,)
# Every run status except a draft; a concurrent reset to WAITING wins.
CASE_SEND_RUN_STATUSES: tuple[str, ...] = (
    TestRunStatus.STARTED,
    TestRunStatus.PAUSED,
    TestRunStatus.DONE,
    TestRunStatus.CANCELLED,
)

_RUN_NOT_STARTED_MESSAGE = (
    "None of the selected cases can be sent: the run has not started."
)
_NOTHING_TO_SEND_MESSAGE = (
    "None of the selected cases can be sent: they are not failed, have no "
    "source TestRail run or were already sent."
)


@dataclass(frozen=True)
class CaseSendOutcome:
    """`sending` cases queued, `checked_now` of them checked by this action,
    `skipped` selected cases that could not be sent."""

    sending: int
    checked_now: int
    skipped: int


def is_case_sendable(pivot: TestRunTestCase) -> bool:
    """Whether an already-loaded pivot can be sent now (no query)."""
    return (
        pivot.status in CASE_SEND_STATUSES
        and pivot.testrail_source_run_id is not None
        and pivot.testrail_source_result_id is None
    )


def is_case_missing_source(pivot: TestRunTestCase) -> bool:
    """Whether a failed case can't be sent because it has no source TestRail
    run (imported from a suite, not a run link); no query."""
    return pivot.status in CASE_SEND_STATUSES and pivot.testrail_source_run_id is None


def list_case_sendable_pivot_ids(test_run: TestRun) -> list[int]:
    """Pivot ids of `test_run` that can be sent now (1 query)."""
    return list(
        test_run.pivot_entries.filter(
            status__in=CASE_SEND_STATUSES,
            testrail_source_run_id__isnull=False,
            testrail_source_result_id__isnull=True,
        ).values_list("id", flat=True)
    )


def start_case_send(
    *,
    test_run: TestRun,
    pivot_ids: Sequence[int],
    user: CustomUser,
) -> CaseSendOutcome:
    """Check the selected sendable cases and queue sending exactly those.

    One transaction takes the run's send slot (locking the run row first,
    then the pivots, like Uncheck and Reset), locks the eligible pivots,
    checks the unchecked ones and clears a previous send's skip reason on the
    others (pressing Send is an explicit retry). A refusal rolls everything
    back, the slot included. The task is dispatched after the commit, so this
    must not run inside an outer transaction. A dispatch failure leaves the
    cases checked (they go with the next send).

    Raises `TestRailNotConfiguredError` or a `CheckedPushError` subclass:
    `RunNotFinishedError` (draft run), `CheckedPushInProgressError`,
    `NothingToSendError`, or `CheckedPushError` (broker unreachable).
    """
    if transaction.get_connection().in_atomic_block:
        # The task would be queued before the slot commits and then drop the
        # send without an error (`get_checked_push_run` misses the slot).
        raise RuntimeError("start_case_send must not run inside a transaction")
    if not test_run.project.has_testrail_settings:
        raise TestRailNotConfiguredError(TESTRAIL_NOT_CONFIGURED_MESSAGE)
    if test_run.status not in CASE_SEND_RUN_STATUSES:
        raise RunNotFinishedError(_RUN_NOT_STARTED_MESSAGE)
    fail_stale_checked_push(test_run)
    selected = set(pivot_ids)
    previous_slot = (
        test_run.testrail_source_push_status,
        test_run.testrail_source_push_started_at,
        test_run.testrail_source_push_error,
    )
    try:
        with transaction.atomic():
            slot_token = take_checked_push_slot(
                test_run,
                run_statuses=CASE_SEND_RUN_STATUSES,
                run_status_message=_RUN_NOT_STARTED_MESSAGE,
            )
            pivots = list(
                test_run.pivot_entries.select_for_update()
                .filter(
                    id__in=selected,
                    status__in=CASE_SEND_STATUSES,
                    testrail_source_run_id__isnull=False,
                    testrail_source_result_id__isnull=True,
                )
                .only("id", "status", "checked_at")
            )
            if not pivots:
                raise NothingToSendError(_NOTHING_TO_SEND_MESSAGE)
            now = timezone.now()
            checked_now = check_locked_pivots(
                [pivot for pivot in pivots if pivot.checked_at is None],
                user=user,
                now=now,
            )
            already_checked = [
                pivot.id for pivot in pivots if pivot.checked_at is not None
            ]
            if already_checked:
                TestRunTestCase.objects.filter(id__in=already_checked).exclude(
                    testrail_source_skip_reason=""
                ).update(testrail_source_skip_reason="", updated_at=now)
    except Exception:
        # The rollback released the slot; keep the instance in step with it.
        (
            test_run.testrail_source_push_status,
            test_run.testrail_source_push_started_at,
            test_run.testrail_source_push_error,
        ) = previous_slot
        raise
    sendable_ids = [pivot.id for pivot in pivots]
    dispatch_checked_push(
        test_run,
        slot_token=slot_token,
        pivot_ids=sendable_ids,
    )
    return CaseSendOutcome(
        sending=len(sendable_ids),
        checked_now=checked_now,
        skipped=len(selected) - len(sendable_ids),
    )


def format_case_send_message(outcome: CaseSendOutcome) -> str:
    """User message such as "Sending 3 cases to TestRail… · 1 skipped"."""
    noun = "case" if outcome.sending == 1 else "cases"
    message = f"Sending {outcome.sending} {noun} to TestRail…"
    if outcome.skipped:
        message += f" · {outcome.skipped} skipped"
    return message
