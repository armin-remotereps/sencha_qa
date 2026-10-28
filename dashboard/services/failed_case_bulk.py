"""Bulk Check / Re-run / Send to TestRail from the Failures page.

The selection is re-resolved on the server through the Failed cases scope of
the user's member projects, so a forged or foreign id is dropped (counted as
skipped). Each run is then handed to the existing per-run service, which owns
its transaction, locks and rules; a refusal for one run does not undo the
others.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from django.db import models

from accounts.models import CustomUser
from dashboard.services.failed_case_filters import (
    FailedCaseFilters,
    failed_case_queryset,
)
from dashboard.services.member_projects import member_project_ids
from projects.exceptions import CheckedPushError, RunCaseCheckError, RunRerunError
from projects.models import TestRun
from projects.run_case_check_services import check_run_cases, format_check_message
from projects.run_rerun_services import format_rerun_message, rerun_test_run_cases
from projects.testrail_checked_push_services import start_checked_push
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
    SEND = "send", "Send checked to TestRail"


@dataclass(frozen=True)
class BulkOutcome:
    """Per-run results (user-safe texts) and the number of dropped ids."""

    successes: tuple[str, ...]
    errors: tuple[str, ...]
    skipped: int


def _run_label(test_run: TestRun) -> str:
    return f"{test_run.project.name} · {test_run.display_name}"


def apply_failed_case_bulk(
    *,
    user: CustomUser,
    action: BulkAction,
    pivot_ids: Sequence[int],
    links_base_url: str,
) -> BulkOutcome:
    """Apply ``action`` to the selected Failed cases, run by run.

    - check: ``check_run_cases`` per run;
    - rerun: only when every selected case is in one run; keeps the run's
      help mode;
    - send: ``start_checked_push`` once per affected run, which sends every
      checked, unsent case of that run (as the run page does).
    """
    selected = set(pivot_ids)
    project_ids = member_project_ids(user)
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
                start_checked_push(test_run=test_run, links_base_url=links_base_url)
                message = "Sending its checked results to TestRail…"
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
