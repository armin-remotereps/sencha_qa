from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import SplitResult, urlsplit

from django.db import transaction

from accounts.models import CustomUser
from auto_tester.celery import app as celery_app
from projects.exceptions import (
    TestRailHostMismatchError,
    TestRailRunImportInProgressError,
    TestRailRunNotFoundError,
    TestRailRunReferenceError,
)
from projects.models import Project, TestCaseUpload, UploadSource, UploadStatus
from projects.testrail_client import TestRailCase, TestRailError, TestRailRun
from projects.testrail_mapping import TestRailFieldMapper
from projects.testrail_services import (
    TestRailNotConfiguredError,
    TestRailUpsertCounts,
    build_testrail_client,
    upsert_test_cases_from_testrail,
)

IMPORT_TESTRAIL_RUN_CASES_TASK = "projects.tasks.import_testrail_run_cases"

# Matches TestCaseUpload.original_filename (CharField max_length=255) — the
# label is stored there.
_RUN_LABEL_MAX = 255
# TestCaseUpload.testrail_run_id is a PositiveIntegerField.
_MAX_RUN_ID = 2_147_483_647
_DEFAULT_PORTS = frozenset({80, 443})

_RUN_ID_RE = re.compile(r"^[Rr]?(\d+)$")
_RUN_QUERY_RE = re.compile(r"^/runs/view/(\d+)(?:[&/].*)?$")
_PLAN_QUERY_RE = re.compile(r"^/plans/view/")
_TEST_QUERY_RE = re.compile(r"^/tests/view/")

_GENERIC_REFERENCE_MESSAGE = (
    "That is not a TestRail run link or run ID. Paste a link like "
    "…/index.php?/runs/view/123, or an ID like R123."
)
_PLAN_REFERENCE_MESSAGE = (
    "That is a TestRail test plan link. Open one of the plan's runs and paste "
    "the run link instead."
)
_TEST_REFERENCE_MESSAGE = (
    "That is a link to a single TestRail test. Paste the link of the run it "
    "belongs to instead."
)
_CREDENTIALS_MESSAGE = (
    "TestRail rejected the stored credentials. Check the email and API key "
    "under Settings."
)
_DISPATCH_FAILED_MESSAGE = "Could not queue the import. Try again in a moment."
_IN_FLIGHT_STATUSES = (UploadStatus.PENDING, UploadStatus.PROCESSING)


# ============================================================================
# REFERENCE PARSING (pure)
# ============================================================================


def _valid_run_id(digits: str) -> int:
    run_id = int(digits)
    if not 0 < run_id <= _MAX_RUN_ID:
        raise TestRailRunReferenceError(_GENERIC_REFERENCE_MESSAGE)
    return run_id


def _split_url(text: str) -> SplitResult:
    if "://" not in text:
        text = f"https://{text}"
    try:
        return urlsplit(text)
    except ValueError as exc:
        raise TestRailRunReferenceError(_GENERIC_REFERENCE_MESSAGE) from exc


def _host_and_port(parts: SplitResult) -> tuple[str, int | None]:
    """Lowercased hostname and the port, with default ports (80/443) as None."""
    try:
        port = parts.port
    except ValueError as exc:
        raise TestRailRunReferenceError(_GENERIC_REFERENCE_MESSAGE) from exc
    hostname = (parts.hostname or "").lower()
    return hostname, None if port in _DEFAULT_PORTS else port


def _display_host(hostname: str, port: int | None) -> str:
    return hostname if port is None else f"{hostname}:{port}"


def parse_testrail_run_reference(raw: str, *, testrail_base_url: str) -> int:
    """Return the run ID from a pasted run URL or ``R123``/``123``.

    The pasted URL's host is only compared with ``testrail_base_url``; it is
    never requested.
    """
    text = raw.strip()
    id_match = _RUN_ID_RE.match(text)
    if id_match is not None:
        return _valid_run_id(id_match.group(1))

    if not text or any(char.isspace() for char in text):
        raise TestRailRunReferenceError(_GENERIC_REFERENCE_MESSAGE)
    pasted = _split_url(text)
    pasted_host, pasted_port = _host_and_port(pasted)
    # A bare word ("foo") parses as a host with nothing after it; it is not a URL.
    if not pasted_host or not (pasted.path or pasted.query):
        raise TestRailRunReferenceError(_GENERIC_REFERENCE_MESSAGE)

    base = urlsplit(testrail_base_url)
    base_host, base_port = _host_and_port(base)
    if (pasted_host, pasted_port) != (base_host, base_port):
        raise TestRailHostMismatchError(
            f"That link points at {_display_host(pasted_host, pasted_port)}, but "
            f"this project is connected to {_display_host(base_host, base_port)}."
        )

    if pasted.path != f"{base.path.rstrip('/')}/index.php":
        raise TestRailRunReferenceError(_GENERIC_REFERENCE_MESSAGE)

    query_match = _RUN_QUERY_RE.match(pasted.query)
    if query_match is not None:
        return _valid_run_id(query_match.group(1))
    if _PLAN_QUERY_RE.match(pasted.query):
        raise TestRailRunReferenceError(_PLAN_REFERENCE_MESSAGE)
    if _TEST_QUERY_RE.match(pasted.query):
        raise TestRailRunReferenceError(_TEST_REFERENCE_MESSAGE)
    raise TestRailRunReferenceError(_GENERIC_REFERENCE_MESSAGE)


def build_testrail_run_import_label(run: TestRailRun) -> str:
    return f"TestRail run R{run.id}: {run.name}"[:_RUN_LABEL_MAX]


# ============================================================================
# IMPORT START
# ============================================================================


def _fetch_run(project: Project, run_id: int) -> TestRailRun:
    try:
        with build_testrail_client(project) as client:
            return client.get_run(run_id)
    except TestRailError as exc:
        if exc.status_code in (400, 404):
            raise TestRailRunNotFoundError(
                f"TestRail run R{run_id} does not exist or is not visible to "
                "this account."
            ) from exc
        if exc.status_code == 401:
            raise TestRailError(_CREDENTIALS_MESSAGE, exc.status_code) from exc
        raise


def _create_upload_unless_in_flight(
    project: Project, user: CustomUser, run: TestRailRun
) -> TestCaseUpload:
    """Create the history row, serialised per project so a double submit of
    the same run cannot start two imports."""
    with transaction.atomic():
        Project.objects.select_for_update().only("id").get(pk=project.pk)
        if TestCaseUpload.objects.filter(
            project=project, testrail_run_id=run.id, status__in=_IN_FLIGHT_STATUSES
        ).exists():
            raise TestRailRunImportInProgressError(
                f"TestRail run R{run.id} is already being imported into this project."
            )
        return TestCaseUpload.objects.create(
            project=project,
            uploaded_by=user,
            original_filename=build_testrail_run_import_label(run),
            source=UploadSource.TESTRAIL_API,
            testrail_run_id=run.id,
            testrail_project_id=run.project_id,
            testrail_suite_id=run.suite_id,
            status=UploadStatus.PROCESSING,
        )


def start_testrail_run_import(
    *, project: Project, user: CustomUser, raw_reference: str
) -> TestCaseUpload:
    """Validate the pasted run, create the history row and dispatch the import."""
    if not project.has_testrail_settings:
        raise TestRailNotConfiguredError(
            "TestRail is not configured for this project. Add the URL, email and API key under Settings."
        )
    run_id = parse_testrail_run_reference(
        raw_reference, testrail_base_url=project.testrail_url
    )
    run = _fetch_run(project, run_id)
    upload = _create_upload_unless_in_flight(project, user, run)
    try:
        result = celery_app.send_task(IMPORT_TESTRAIL_RUN_CASES_TASK, args=[upload.id])
    except Exception as exc:
        # Broker unreachable: fail the row so the in-flight guard does not
        # block every later attempt at this run.
        upload.status = UploadStatus.FAILED
        upload.error_message = _DISPATCH_FAILED_MESSAGE
        upload.save(update_fields=["status", "error_message", "updated_at"])
        raise TestRailError(_DISPATCH_FAILED_MESSAGE, 0) from exc
    upload.celery_task_id = str(result.id)
    upload.save(update_fields=["celery_task_id", "updated_at"])
    return upload


# ============================================================================
# IMPORT WORKER
# ============================================================================


def _unique_by_case_id(cases: list[TestRailCase]) -> list[TestRailCase]:
    """A run can hold the same case more than once; keep its first occurrence."""
    seen: set[int] = set()
    unique: list[TestRailCase] = []
    for case in cases:
        if case.id not in seen:
            seen.add(case.id)
            unique.append(case)
    return unique


def import_testrail_run_cases(
    upload: TestCaseUpload, *, notify_progress: Callable[[], None]
) -> TestRailUpsertCounts:
    """Fetch the run's cases and upsert them. Raises TestRailError /
    TestRailNotConfiguredError; the caller broadcasts via ``notify_progress``.
    """
    run_id = upload.testrail_run_id
    if run_id is None:
        raise TestRailError("Import row is missing its TestRail run id.", 0)

    with build_testrail_client(upload.project) as client:
        mapper = TestRailFieldMapper.from_vocabularies(
            client.get_case_types(), client.get_priorities()
        )
        cases = _unique_by_case_id(list(client.iter_run_cases(run_id)))

    if not cases:
        raise TestRailError(f"TestRail run R{run_id} has no tests.", 0)

    upload.total_cases = len(cases)
    upload.save(update_fields=["total_cases", "updated_at"])
    notify_progress()

    def on_batch_processed(processed: int, updated: int) -> None:
        upload.processed_cases = processed
        upload.updated_cases = updated
        upload.save(update_fields=["processed_cases", "updated_cases", "updated_at"])
        notify_progress()

    return upsert_test_cases_from_testrail(
        upload=upload,
        project=upload.project,
        cases=cases,
        mapper=mapper,
        batch_size=50,
        progress_callback=on_batch_processed,
    )
