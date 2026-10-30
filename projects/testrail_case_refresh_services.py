from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from django.core.cache import cache
from django.db import transaction
from django.db.models import QuerySet

from projects.exceptions import (
    TestCaseNoTestRailIdError,
    TestCaseRefreshBusyError,
    TestCaseRefreshError,
    TestCaseRefreshThrottledError,
    TestRailCaseNotFoundError,
)
from projects.models import (
    ACTIVE_RUN_STATUSES,
    FINISHED_CASE_STATUSES,
    Project,
    TestCase,
    TestCaseData,
    TestRunTestCase,
)
from projects.testrail_client import TestRailCase, TestRailError
from projects.testrail_mapping import parse_testrail_case_id
from projects.testrail_services import (
    TESTRAIL_CASE_FIELDS,
    apply_testrail_case_data,
    build_testrail_client,
    get_testrail_field_mapper,
)

logger = logging.getLogger(__name__)

REFRESH_THROTTLE_LIMIT = 30
REFRESH_THROTTLE_WINDOW_SECONDS = 60

_NO_ID_MESSAGE = "This test case has no TestRail case ID to refresh from."
_THROTTLED_MESSAGE = (
    "Too many TestRail refreshes in this project. Wait a minute and try again."
)
_CREDENTIALS_MESSAGE = (
    "TestRail rejected the stored credentials. Check the email and API key "
    "under Settings."
)
_RATE_LIMITED_MESSAGE = "TestRail is rate limiting requests. Try again in a minute."
_UNREACHABLE_MESSAGE = "Could not reach TestRail. Try again in a moment."

_FIELD_LABELS: Mapping[str, str] = {
    "title": "title",
    "template": "template",
    "type": "type",
    "priority": "priority",
    "estimate": "estimate",
    "references": "references",
    "preconditions": "preconditions",
    "steps": "steps",
    "expected": "expected result",
}


@dataclass(frozen=True)
class TestCaseRefreshOutcome:
    testrail_case_id: int
    # Changed fields across all refreshed rows, in TESTRAIL_CASE_FIELDS order.
    changed_fields: tuple[str, ...]
    rows_updated: int
    # TestRail now files the case under another suite than the stored one.
    suite_moved: bool


def can_refresh_from_testrail(project: Project, test_case: TestCase) -> bool:
    """Whether to offer the refresh: TestRail settings and a parseable case id."""
    return (
        project.has_testrail_settings
        and parse_testrail_case_id(test_case.testrail_id) is not None
    )


def refresh_test_case_from_testrail(
    *, project: Project, test_case: TestCase
) -> TestCaseRefreshOutcome:
    """Overwrite the case's text fields with the case's current TestRail version.

    Updates every case of the project with the same stored ``testrail_id``, as
    an import does. Refused while any of them is still being run (not finished
    in a Running or Needs-help run): its saved plan came from the old text.
    Order fields and the source run are kept. Raises TestRailNotConfiguredError
    or a TestCaseRefreshError subclass; the message is safe to show.
    """
    case_id = parse_testrail_case_id(test_case.testrail_id)
    if case_id is None:
        raise TestCaseNoTestRailIdError(_NO_ID_MESSAGE)
    label = f"C{case_id}"
    siblings = TestCase.objects.filter(
        project=project, testrail_id=test_case.testrail_id
    )
    _check_throttle(project.id)
    _ensure_not_running(siblings, label)
    remote, data = _fetch_case(project, case_id, label)
    with transaction.atomic():
        rows = list(siblings.select_for_update().order_by("pk"))
        _ensure_not_running(siblings, label)
        changed, to_update = _diff_rows(rows, data)
        if to_update:
            TestCase.objects.bulk_update(
                to_update, [*TESTRAIL_CASE_FIELDS, "updated_at"]
            )
    return TestCaseRefreshOutcome(
        testrail_case_id=case_id,
        changed_fields=tuple(f for f in TESTRAIL_CASE_FIELDS if f in changed),
        rows_updated=len(to_update),
        suite_moved=_suite_moved(rows, remote),
    )


def format_refresh_message(outcome: TestCaseRefreshOutcome) -> str:
    label = f"C{outcome.testrail_case_id}"
    if not outcome.changed_fields:
        message = f"{label} is already up to date with TestRail."
    else:
        where = (
            f" in {outcome.rows_updated} test cases" if outcome.rows_updated > 1 else ""
        )
        fields = _join_labels([_FIELD_LABELS[f] for f in outcome.changed_fields])
        message = f"Refreshed {label} from TestRail{where}: {fields} changed."
    if outcome.suite_moved:
        message += (
            f" {label} is now in another TestRail suite; re-import that suite "
            "to update its order."
        )
    return message


def _join_labels(labels: Sequence[str]) -> str:
    if len(labels) == 1:
        return labels[0]
    return f"{', '.join(labels[:-1])} and {labels[-1]}"


def _check_throttle(project_id: int) -> None:
    key = f"projects:testrail-case-refresh:{project_id}"
    cache.add(key, 0, timeout=REFRESH_THROTTLE_WINDOW_SECONDS)
    try:
        attempts = cache.incr(key)
    except ValueError:
        # The key expired between add and incr: this is the window's first refresh.
        cache.add(key, 1, timeout=REFRESH_THROTTLE_WINDOW_SECONDS)
        attempts = 1
    if attempts > REFRESH_THROTTLE_LIMIT:
        raise TestCaseRefreshThrottledError(_THROTTLED_MESSAGE)


def _ensure_not_running(siblings: QuerySet[TestCase], label: str) -> None:
    running = (
        TestRunTestCase.objects.filter(
            test_case__in=siblings, test_run__status__in=ACTIVE_RUN_STATUSES
        )
        .exclude(status__in=FINISHED_CASE_STATUSES)
        .exists()
    )
    if running:
        raise TestCaseRefreshBusyError(
            f"{label} is still being run. Refresh it after the run finishes, or "
            "once the case has a result."
        )


def _fetch_case(
    project: Project, case_id: int, label: str
) -> tuple[TestRailCase, TestCaseData]:
    """One TestRail call with a warm vocabulary cache; never sleeps on a 429."""
    try:
        with build_testrail_client(project, max_rate_limit_retries=0) as client:
            remote = client.get_case(case_id)
            mapper = get_testrail_field_mapper(project, client)
    except TestRailError as exc:
        logger.info(
            "TestRail refresh of a case in project %s failed with status %s",
            project.id,
            exc.status_code,
        )
        if exc.status_code in (400, 404):
            raise TestRailCaseNotFoundError(
                f"{label} does not exist in TestRail any more, or this account "
                "cannot see it."
            ) from exc
        raise TestCaseRefreshError(_error_message(exc, label)) from exc
    return remote, mapper.to_test_case_data(remote)


def _error_message(exc: TestRailError, label: str) -> str:
    if exc.status_code == 401:
        return _CREDENTIALS_MESSAGE
    if exc.status_code == 403:
        return f"The TestRail account has no access to {label}."
    if exc.status_code == 429:
        return _RATE_LIMITED_MESSAGE
    if exc.status_code == 0:
        return _UNREACHABLE_MESSAGE
    return f"TestRail returned an error: {exc.message}"


def _diff_rows(
    rows: Sequence[TestCase], data: TestCaseData
) -> tuple[set[str], list[TestCase]]:
    """Apply ``data`` to rows that differ; returns changed fields and those rows."""
    changed: set[str] = set()
    to_update: list[TestCase] = []
    for row in rows:
        fields = {
            field
            for field in TESTRAIL_CASE_FIELDS
            if getattr(row, field) != getattr(data, field)
        }
        if fields:
            changed |= fields
            apply_testrail_case_data(row, data)
            to_update.append(row)
    return changed, to_update


def _suite_moved(rows: Sequence[TestCase], remote: TestRailCase) -> bool:
    if remote.suite_id is None:
        return False
    return any(
        row.testrail_suite_id is not None and row.testrail_suite_id != remote.suite_id
        for row in rows
    )
