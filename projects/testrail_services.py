from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from auto_tester.celery import app as celery_app
from projects.models import (
    Project,
    TestCase,
    TestCaseData,
    TestCaseUpload,
    UploadSource,
    UploadStatus,
)
from projects.secrets import SecretDecryptionError, decrypt_secret, encrypt_secret
from projects.testrail_client import (
    TestRailCase,
    TestRailClient,
    TestRailError,
    TestRailProject,
    TestRailSuite,
)
from projects.testrail_mapping import TestRailFieldMapper

logger = logging.getLogger(__name__)

TESTRAIL_PROJECTS_CACHE_TTL = 300
_API_KEY_HINT_LENGTH = 3


class TestRailNotConfiguredError(Exception):
    """Raised when a TestRail operation runs on a project without settings."""


def testrail_projects_cache_key(project_id: int) -> str:
    return f"testrail_projects:{project_id}"


# ============================================================================
# SETTINGS
# ============================================================================


def save_testrail_settings(
    *, project: Project, url: str, email: str, api_key: str | None
) -> Project:
    """Store TestRail connection settings. api_key=None keeps the stored key."""
    project.testrail_url = url.strip().rstrip("/")
    project.testrail_email = email.strip()
    update_fields = ["testrail_url", "testrail_email", "updated_at"]
    if api_key is not None:
        project.testrail_api_key_encrypted = encrypt_secret(api_key)
        update_fields.append("testrail_api_key_encrypted")
    project.save(update_fields=update_fields)
    cache.delete(testrail_projects_cache_key(project.id))
    return project


def clear_testrail_settings(project: Project) -> None:
    project.testrail_url = ""
    project.testrail_email = ""
    project.testrail_api_key_encrypted = ""
    project.save(
        update_fields=[
            "testrail_url",
            "testrail_email",
            "testrail_api_key_encrypted",
            "updated_at",
        ]
    )
    cache.delete(testrail_projects_cache_key(project.id))


def _decrypt_api_key(project: Project) -> str:
    if not project.testrail_api_key_encrypted:
        return ""
    try:
        return decrypt_secret(project.testrail_api_key_encrypted)
    except SecretDecryptionError:
        logger.warning(
            "TestRail API key for project %s cannot be decrypted; treating as unset",
            project.id,
        )
        return ""


def get_testrail_api_key_hint(project: Project) -> str:
    """Last few characters of the stored key, for the settings form hint."""
    return _decrypt_api_key(project)[-_API_KEY_HINT_LENGTH:]


def build_testrail_client(project: Project) -> TestRailClient:
    api_key = _decrypt_api_key(project)
    if not (project.testrail_url and project.testrail_email and api_key):
        raise TestRailNotConfiguredError(
            "TestRail is not configured for this project. Add the URL, email and API key under Settings."
        )
    return TestRailClient(
        url=project.testrail_url, email=project.testrail_email, api_key=api_key
    )


@dataclass(frozen=True)
class TestRailConnectionResult:
    ok: bool
    message: str
    project_count: int = 0


def check_testrail_connection(project: Project) -> TestRailConnectionResult:
    try:
        with build_testrail_client(project) as client:
            projects = client.get_projects()
    except TestRailNotConfiguredError as exc:
        return TestRailConnectionResult(ok=False, message=str(exc))
    except TestRailError as exc:
        return TestRailConnectionResult(ok=False, message=exc.message)
    count = len(projects)
    return TestRailConnectionResult(
        ok=True,
        message=f"Connected. {count} TestRail project{'s' if count != 1 else ''} visible to this account.",
        project_count=count,
    )


# ============================================================================
# PICKER
# ============================================================================


@dataclass(frozen=True)
class TestRailPickerState:
    configured: bool
    projects: list[TestRailProject]
    error: str = ""


def _picker_sort_key(project: TestRailProject) -> tuple[bool, str]:
    return (project.is_completed, project.name.casefold())


def _fetch_testrail_projects(project: Project) -> list[TestRailProject]:
    """Cached, sorted TestRail project list. Raises TestRailError."""
    key = testrail_projects_cache_key(project.id)
    cached = cache.get(key)
    if isinstance(cached, list):
        return [TestRailProject(**item) for item in cached]
    with build_testrail_client(project) as client:
        projects = sorted(client.get_projects(), key=_picker_sort_key)
    cache.set(key, [asdict(item) for item in projects], TESTRAIL_PROJECTS_CACHE_TTL)
    return projects


def get_testrail_picker_state(project: Project) -> TestRailPickerState:
    if not project.has_testrail_settings:
        return TestRailPickerState(configured=False, projects=[])
    try:
        return TestRailPickerState(
            configured=True, projects=_fetch_testrail_projects(project)
        )
    except (TestRailError, TestRailNotConfiguredError) as exc:
        return TestRailPickerState(configured=True, projects=[], error=str(exc))


# ============================================================================
# IMPORT TARGET RESOLUTION
# ============================================================================


@dataclass(frozen=True)
class TestRailImportResolution:
    testrail_project: TestRailProject
    suites: list[TestRailSuite]

    @property
    def single_suite(self) -> TestRailSuite | None:
        return self.suites[0] if len(self.suites) == 1 else None


@dataclass(frozen=True)
class TestRailImportTarget:
    testrail_project: TestRailProject
    suite: TestRailSuite


def _find_testrail_project(
    project: Project, testrail_project_id: int
) -> TestRailProject:
    for candidate in _fetch_testrail_projects(project):
        if candidate.id == testrail_project_id:
            return candidate
    raise TestRailError(
        f"TestRail project {testrail_project_id} is not visible to this account.", 404
    )


def resolve_testrail_import(
    project: Project, testrail_project_id: int
) -> TestRailImportResolution:
    testrail_project = _find_testrail_project(project, testrail_project_id)
    with build_testrail_client(project) as client:
        suites = client.get_suites(testrail_project.id)
    if not suites:
        raise TestRailError(
            f"TestRail project “{testrail_project.name}” has no suites.", 404
        )
    return TestRailImportResolution(testrail_project=testrail_project, suites=suites)


def resolve_testrail_import_suite(
    project: Project, testrail_project_id: int, suite_id: int
) -> TestRailImportTarget:
    resolution = resolve_testrail_import(project, testrail_project_id)
    for suite in resolution.suites:
        if suite.id == suite_id:
            return TestRailImportTarget(
                testrail_project=resolution.testrail_project, suite=suite
            )
    raise TestRailError(
        f"Suite {suite_id} does not belong to TestRail project “{resolution.testrail_project.name}”.",
        404,
    )


def build_testrail_import_label(
    testrail_project: TestRailProject, suite: TestRailSuite
) -> str:
    return f"TestRail: {testrail_project.name} / {suite.name}"


# ============================================================================
# IMPORT START + UPSERT
# ============================================================================


def start_testrail_import(
    *, project: Project, user: CustomUser, target: TestRailImportTarget
) -> TestCaseUpload:
    """Create the history row and dispatch the import task."""
    upload = TestCaseUpload.objects.create(
        project=project,
        uploaded_by=user,
        original_filename=build_testrail_import_label(
            target.testrail_project, target.suite
        ),
        source=UploadSource.TESTRAIL_API,
        testrail_project_id=target.testrail_project.id,
        testrail_suite_id=target.suite.id,
        status=UploadStatus.PROCESSING,
    )
    result = celery_app.send_task(
        "projects.tasks.import_testrail_cases", args=[upload.id]
    )
    upload.celery_task_id = str(result.id)
    upload.save(update_fields=["celery_task_id", "updated_at"])
    return upload


@dataclass(frozen=True)
class TestRailUpsertCounts:
    created: int
    updated: int


_UPSERT_FIELDS = (
    "title",
    "template",
    "type",
    "priority",
    "estimate",
    "references",
    "preconditions",
    "steps",
    "expected",
)


def _apply_data(test_case: TestCase, data: TestCaseData) -> None:
    for field in _UPSERT_FIELDS:
        setattr(test_case, field, getattr(data, field))
    # bulk_update does not apply auto_now, so stamp it explicitly.
    test_case.updated_at = timezone.now()


def _new_test_case(
    project: Project, upload: TestCaseUpload, data: TestCaseData
) -> TestCase:
    test_case = TestCase(project=project, upload=upload, testrail_id=data.testrail_id)
    _apply_data(test_case, data)
    return test_case


def _upsert_batch(
    *, upload: TestCaseUpload, project: Project, batch: Sequence[TestCaseData]
) -> TestRailUpsertCounts:
    ids = [data.testrail_id for data in batch]
    existing_by_id: dict[str, list[TestCase]] = {}
    for test_case in TestCase.objects.filter(project=project, testrail_id__in=ids):
        existing_by_id.setdefault(test_case.testrail_id, []).append(test_case)

    to_update: list[TestCase] = []
    to_create: list[TestCase] = []
    updated = 0
    for data in batch:
        matches = existing_by_id.get(data.testrail_id, [])
        if not matches:
            to_create.append(_new_test_case(project, upload, data))
            continue
        updated += 1
        for test_case in matches:
            _apply_data(test_case, data)
            to_update.append(test_case)

    with transaction.atomic():
        if to_update:
            TestCase.objects.bulk_update(to_update, [*_UPSERT_FIELDS, "updated_at"])
        if to_create:
            TestCase.objects.bulk_create(to_create)
    return TestRailUpsertCounts(created=len(to_create), updated=updated)


def upsert_test_cases_from_testrail(
    *,
    upload: TestCaseUpload,
    project: Project,
    cases: Sequence[TestRailCase],
    mapper: TestRailFieldMapper,
    batch_size: int = 50,
    progress_callback: Callable[[int, int], None] | None = None,
) -> TestRailUpsertCounts:
    """Create missing cases (linked to `upload`) and update existing ones by testrail_id.

    progress_callback receives cumulative (processed, updated) after each batch.
    """
    created_total = 0
    updated_total = 0
    for start in range(0, len(cases), batch_size):
        batch = [
            mapper.to_test_case_data(case) for case in cases[start : start + batch_size]
        ]
        counts = _upsert_batch(upload=upload, project=project, batch=batch)
        created_total += counts.created
        updated_total += counts.updated
        if progress_callback is not None:
            progress_callback(created_total + updated_total, updated_total)
    return TestRailUpsertCounts(created=created_total, updated=updated_total)
