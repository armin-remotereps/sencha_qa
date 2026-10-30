"""One-off backfill of TestRail test ids for cases imported from a run link.

Test cases and run cases that know their source TestRail run but not the test
in it (imported before imports kept test ids) get it from that run's
``get_tests``, so "Open in TestRail" opens the test instead of the run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Q

from projects.models import Project, TestCase, TestRunTestCase
from projects.testrail_client import TestRailClient, TestRailError
from projects.testrail_mapping import parse_testrail_case_id
from projects.testrail_services import (
    TestRailNotConfiguredError,
    build_testrail_client,
)

logger = logging.getLogger(__name__)

_BULK_BATCH_SIZE = 500


@dataclass
class TestIdBackfillResult:
    project_id: int
    project_name: str
    test_cases_updated: int = 0
    run_cases_updated: int = 0
    # TestRail runs that are gone (400/404); their cases keep the run link.
    missing_runs: list[int] = field(default_factory=list)
    error: str = ""


def _missing_test_case_runs(project: Project) -> set[int]:
    run_ids = (
        TestCase.objects.filter(
            project=project,
            testrail_source_run_id__isnull=False,
            testrail_source_test_id__isnull=True,
        )
        .values_list("testrail_source_run_id", flat=True)
        .distinct()
    )
    return {run_id for run_id in run_ids if run_id is not None}


def _missing_run_case_runs(project: Project) -> set[int]:
    run_ids = (
        TestRunTestCase.objects.filter(
            test_run__project=project,
            testrail_source_run_id__isnull=False,
            testrail_source_test_id__isnull=True,
        )
        .values_list("testrail_source_run_id", flat=True)
        .distinct()
    )
    return {run_id for run_id in run_ids if run_id is not None}


def _fill_test_cases(project: Project, run_id: int, test_ids: dict[int, int]) -> int:
    rows = list(
        TestCase.objects.filter(
            project=project,
            testrail_source_run_id=run_id,
            testrail_source_test_id__isnull=True,
        )
        # Locked so an import moving a case to another run waits for this write.
        .select_for_update().only("id", "testrail_id")
    )
    changed: list[TestCase] = []
    for test_case in rows:
        case_id = parse_testrail_case_id(test_case.testrail_id)
        test_id = test_ids.get(case_id) if case_id is not None else None
        if test_id is not None:
            test_case.testrail_source_test_id = test_id
            changed.append(test_case)
    TestCase.objects.bulk_update(
        changed, ["testrail_source_test_id"], batch_size=_BULK_BATCH_SIZE
    )
    return len(changed)


def _fill_run_cases(project: Project, run_id: int, test_ids: dict[int, int]) -> int:
    rows = list(
        TestRunTestCase.objects.filter(
            test_run__project=project,
            testrail_source_run_id=run_id,
            testrail_source_test_id__isnull=True,
        )
        .only("id", "test_case__testrail_id")
        .select_related("test_case")
    )
    changed: list[TestRunTestCase] = []
    for pivot in rows:
        case_id = parse_testrail_case_id(pivot.test_case.testrail_id)
        test_id = test_ids.get(case_id) if case_id is not None else None
        if test_id is not None:
            pivot.testrail_source_test_id = test_id
            changed.append(pivot)
    TestRunTestCase.objects.bulk_update(
        changed, ["testrail_source_test_id"], batch_size=_BULK_BATCH_SIZE
    )
    return len(changed)


def _backfill_run(
    client: TestRailClient,
    project: Project,
    run_id: int,
    result: TestIdBackfillResult,
) -> None:
    try:
        test_ids = client.get_run_test_ids(run_id)
    except TestRailError as exc:
        if exc.status_code in (400, 404):
            result.missing_runs.append(run_id)
            return
        raise
    # Only rows still missing a test id are read. A send racing this write
    # records the same test (one test per case in a run), so either wins.
    with transaction.atomic():
        result.test_cases_updated += _fill_test_cases(project, run_id, test_ids)
        result.run_cases_updated += _fill_run_cases(project, run_id, test_ids)


def backfill_project_test_ids(project: Project) -> TestIdBackfillResult:
    """Fill missing source test ids of one project, one TestRail call per run."""
    result = TestIdBackfillResult(project_id=project.pk, project_name=project.name)
    run_ids = sorted(_missing_test_case_runs(project) | _missing_run_case_runs(project))
    if not run_ids:
        return result
    try:
        with build_testrail_client(project) as client:
            for run_id in run_ids:
                _backfill_run(client, project, run_id, result)
    except (TestRailNotConfiguredError, TestRailError) as exc:
        result.error = exc.message
        logger.warning(
            "TestRail test id backfill stopped for project %s: %s",
            project.pk,
            exc.message,
        )
    return result


def backfill_testrail_test_ids(
    project_id: int | None = None,
) -> list[TestIdBackfillResult]:
    """Backfill every project with TestRail settings (or only ``project_id``)."""
    projects = Project.objects.exclude(
        Q(testrail_url="") | Q(testrail_email="") | Q(testrail_api_key_encrypted="")
    ).order_by("pk")
    if project_id is not None:
        projects = projects.filter(pk=project_id)
    return [backfill_project_test_ids(project) for project in projects]
