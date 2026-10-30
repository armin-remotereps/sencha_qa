"""Bulk Check / Re-run / Send to TestRail from the Failures page.

The selection is re-resolved on the server through the Failed cases scope of
the user's member projects, so a forged or foreign id is dropped (counted as
skipped). Each run is then handed to the existing per-run service, which owns
its transaction, locks and rules; a refusal for one run does not undo the
others.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from django.db import models

from accounts.models import CustomUser
from dashboard.services.failed_case_filters import (
    FailedCaseFilters,
    failed_case_queryset,
)
from projects.exceptions import CheckedPushError, RunCaseCheckError, RunRerunError
from projects.models import TestRun, TestRunTestCase
from projects.run_case_check_services import check_run_cases, format_check_message
from projects.run_rerun_services import format_rerun_message, rerun_test_run_cases
from projects.testrail_case_send_services import (
    format_case_send_message,
    is_case_missing_source,
    is_case_sendable,
    start_case_send,
)
from projects.testrail_services import TestRailNotConfiguredError

# At most this many cases per bulk request (one page at the largest size).
BULK_MAX_CASES = 100

_NOTHING_SELECTED = (
    "None of the selected cases are failed cases in your projects any more. "
    "Reload the page and try again."
)
_RERUN_ONE_RUN = (
    "Re-run works on cases from one run at a time. Select cases from a single run."
)


class BulkAction(models.TextChoices):
    CHECK = "check", "Check"
    RERUN = "rerun", "Re-run"
    SEND = "send", "Send to TestRail"


@dataclass(frozen=True)
class BulkOutcome:
    """Per-run results (user-safe texts) and the number of dropped ids."""

    successes: tuple[str, ...]
    errors: tuple[str, ...]
    skipped: int


def _run_label(test_run: TestRun) -> str:
    return f"{test_run.project.name} · {test_run.display_name}"


def failed_case_sendable_ids(
    pivots: Iterable[TestRunTestCase], writable_project_ids: Sequence[int]
) -> list[int]:
    """Ids of the listed rows that Send would accept now (no query).

    ``pivots`` come from ``failed_case_list_queryset`` (project joined,
    ``testrail_configured`` annotated); a row qualifies in a writable project
    with TestRail settings when ``is_case_sendable``. Display only: the POST
    re-checks everything under lock.
    """
    writable = set(writable_project_ids)
    return [
        pivot.id
        for pivot in pivots
        if pivot.test_run.project_id in writable
        # Annotation of ``failed_case_list_queryset``, unknown to the stubs.
        and bool(getattr(pivot, "testrail_configured", False))
        and is_case_sendable(pivot)
    ]


def failed_case_no_source_ids(pivots: Iterable[TestRunTestCase]) -> list[int]:
    """Ids of the listed rows that can't be sent only because they have no
    source TestRail run (project has TestRail settings; no query)."""
    return [
        pivot.id
        for pivot in pivots
        # Annotation of ``failed_case_list_queryset``, unknown to the stubs.
        if bool(getattr(pivot, "testrail_configured", False))
        and is_case_missing_source(pivot)
    ]


def apply_failed_case_bulk(
    *,
    user: CustomUser,
    project_ids: Sequence[int],
    action: BulkAction,
    pivot_ids: Sequence[int],
) -> BulkOutcome:
    """Apply ``action`` to the selected Failed cases, run by run.

    ``project_ids`` are the user's writable, non-archived projects (the view
    already loaded them); every selected id is re-resolved against them here.

    - check: ``check_run_cases`` per run;
    - rerun: only when every selected case is in one run; keeps the run's
      help mode;
    - send: ``start_case_send`` once per affected run with only that run's
      selected cases, also while the run is still running: it checks the
      unchecked ones (by ``user``) and sends exactly those to the run's source
      TestRail run. Each call owns its transaction and dispatches after its
      commit, so this function must not run inside an outer transaction.
    """
    selected = set(pivot_ids)
    resolved = list(
        failed_case_queryset(project_ids, FailedCaseFilters())
        .filter(id__in=selected)
        .order_by()
        .values_list("id", "test_run_id")
    )
    skipped = len(selected) - len(resolved)
    if not resolved:
        # The error already says every selected case was dropped.
        return BulkOutcome(successes=(), errors=(_NOTHING_SELECTED,), skipped=0)

    ids_by_run: dict[int, list[int]] = defaultdict(list)
    for pivot_id, run_id in resolved:
        ids_by_run[run_id].append(pivot_id)
    if action == BulkAction.RERUN and len(ids_by_run) > 1:
        return BulkOutcome(successes=(), errors=(_RERUN_ONE_RUN,), skipped=skipped)

    runs = TestRun.objects.select_related("project").in_bulk(list(ids_by_run))
    successes: list[str] = []
    errors: list[str] = []
    for run_id in sorted(ids_by_run, reverse=True):
        test_run = runs.get(run_id)
        if test_run is None:
            # Deleted between the two queries.
            skipped += len(ids_by_run[run_id])
            continue
        label = _run_label(test_run)
        run_pivot_ids = ids_by_run[run_id]
        try:
            if action == BulkAction.CHECK:
                check_outcome = check_run_cases(
                    test_run=test_run, pivot_ids=run_pivot_ids, user=user
                )
                message = format_check_message(check_outcome, verb="Checked")
            elif action == BulkAction.RERUN:
                rerun_outcome = rerun_test_run_cases(
                    test_run=test_run, pivot_ids=run_pivot_ids, user=user
                )
                message = format_rerun_message(rerun_outcome)
            else:
                send_outcome = start_case_send(
                    test_run=test_run,
                    pivot_ids=run_pivot_ids,
                    user=user,
                )
                message = format_case_send_message(send_outcome)
        except (
            RunCaseCheckError,
            RunRerunError,
            CheckedPushError,
            TestRailNotConfiguredError,
        ) as exc:
            errors.append(f"{label}: {exc.message}")
        else:
            successes.append(f"{label}: {message}")
    return BulkOutcome(
        successes=tuple(successes), errors=tuple(errors), skipped=skipped
    )
