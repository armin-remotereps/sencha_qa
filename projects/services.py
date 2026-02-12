from __future__ import annotations

import base64
import dataclasses
import html
import logging
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import asdict

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import UploadedFile
from django.core.paginator import Page, Paginator
from django.db import transaction
from django.db.models import QuerySet

from accounts.models import CustomUser
from agents.services.agent_loop import build_agent_config, run_agent
from agents.types import (
    AgentConfig,
    AgentResult,
    AgentStopReason,
    ChatMessage,
    ScreenshotCallback,
)
from environments.services.docker_client import (
    close_docker_client,
    get_docker_client,
)
from environments.services.orchestration import (
    provision_environment,
    teardown_environment,
)
from projects.models import (
    ParsedTestCase,
    Project,
    Tag,
    TestCase,
    TestCaseData,
    TestCaseUpload,
    TestRun,
    TestRunScreenshot,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
    UploadStatus,
)

logger = logging.getLogger(__name__)


@transaction.atomic
def create_project(*, user: CustomUser, name: str, tag_names: list[str]) -> Project:
    project = Project.objects.create(name=name)
    _sync_tags(project, tag_names)
    project.members.add(user)
    return project


@transaction.atomic
def update_project(*, project: Project, name: str, tag_names: list[str]) -> Project:
    project.name = name
    project.save()
    _sync_tags(project, tag_names)
    return project


def archive_project(project: Project) -> None:
    project.archived = True
    project.save()


def unarchive_project(project: Project) -> None:
    project.archived = False
    project.save()


def get_project_for_user(project_id: int, user: CustomUser) -> Project | None:
    try:
        return Project.objects.filter(id=project_id, archived=False, members=user).get()
    except Project.DoesNotExist:
        return None


def get_project_by_id(project_id: int, user: CustomUser) -> Project | None:
    try:
        return Project.objects.filter(id=project_id, members=user).get()
    except Project.DoesNotExist:
        return None


def list_projects_for_user(
    *,
    user: CustomUser,
    search: str | None,
    tag_filter: str | None,
    page: int,
    per_page: int,
) -> Page[Project]:
    qs: QuerySet[Project] = Project.objects.filter(
        members=user, archived=False
    ).order_by("-created_at")

    if search:
        qs = qs.filter(name__icontains=search)

    if tag_filter:
        qs = qs.filter(tags__name=tag_filter)

    paginator: Paginator[Project] = Paginator(qs, per_page)
    return paginator.get_page(page)


def get_all_tags_for_user(user: CustomUser) -> QuerySet[Tag]:
    return (
        Tag.objects.filter(projects__members=user, projects__archived=False)
        .distinct()
        .order_by("name")
    )


def _normalize_tag_names(tag_names: list[str]) -> list[str]:
    return list({name.strip().lower() for name in tag_names if name.strip()})


def _get_or_create_tags(names: list[str]) -> list[Tag]:
    tags: list[Tag] = []
    for name in names:
        tag, _ = Tag.objects.get_or_create(name=name)
        tags.append(tag)
    return tags


def _sync_tags(project: Project, tag_names: list[str]) -> None:
    normalized = _normalize_tag_names(tag_names)
    tags = _get_or_create_tags(normalized)
    project.tags.set(tags)


# ============================================================================
# TEST CASE SERVICES
# ============================================================================


def create_test_case(*, project: Project, data: TestCaseData) -> TestCase:
    return TestCase.objects.create(project=project, **asdict(data))


def update_test_case(*, test_case: TestCase, data: TestCaseData) -> TestCase:
    for field, value in asdict(data).items():
        setattr(test_case, field, value)
    test_case.save()
    return test_case


def delete_test_case(test_case: TestCase) -> None:
    test_case.delete()


def get_test_case_for_project(test_case_id: int, project: Project) -> TestCase | None:
    try:
        return TestCase.objects.filter(id=test_case_id, project=project).get()
    except TestCase.DoesNotExist:
        return None


def list_test_cases_for_project(
    *,
    project: Project,
    search: str | None,
    page: int,
    per_page: int,
    upload_id: int | None = None,
) -> Page[TestCase]:
    """Return a paginated list of test cases for a project, optionally filtered by upload."""
    qs: QuerySet[TestCase] = TestCase.objects.filter(project=project).order_by(
        "-created_at"
    )

    if search:
        qs = qs.filter(title__icontains=search)

    if upload_id is not None:
        qs = qs.filter(upload_id=upload_id)

    paginator: Paginator[TestCase] = Paginator(qs, per_page)
    return paginator.get_page(page)


# ============================================================================
# UPLOAD MANAGEMENT SERVICES
# ============================================================================


def create_upload(
    *, project: Project, user: CustomUser, file: UploadedFile
) -> TestCaseUpload:
    """Save the uploaded file and create a TestCaseUpload record."""
    return TestCaseUpload.objects.create(
        project=project,
        uploaded_by=user,
        original_filename=file.name or "",
        file=file,
    )


def start_upload_processing(upload: TestCaseUpload) -> str:
    """Dispatch Celery task, save task_id on upload, set status to processing.

    Returns the Celery task id.
    """
    from projects.tasks import process_xml_upload

    result = process_xml_upload.delay(upload.id)
    upload.celery_task_id = result.id
    upload.status = UploadStatus.PROCESSING
    upload.save()
    return str(result.id)


def cancel_upload_processing(upload: TestCaseUpload) -> None:
    """Revoke Celery task if running, delete partial test cases, set status cancelled."""
    from auto_tester.celery import app as celery_app

    celery_app.control.revoke(upload.celery_task_id, terminate=True)
    upload.test_cases.all().delete()
    upload.status = UploadStatus.CANCELLED
    upload.save()


def delete_upload(upload: TestCaseUpload) -> None:
    """Cancel if processing, delete file if exists, then delete upload record."""
    if upload.status == UploadStatus.PROCESSING:
        cancel_upload_processing(upload)

    if upload.file:
        upload.file.delete(save=False)

    upload.delete()


def get_upload_for_project(upload_id: int, project: Project) -> TestCaseUpload | None:
    """Get upload by id for a specific project, or None."""
    try:
        return TestCaseUpload.objects.filter(id=upload_id, project=project).get()
    except TestCaseUpload.DoesNotExist:
        return None


def list_uploads_for_project(
    *, project: Project, page: int, per_page: int
) -> Page[TestCaseUpload]:
    """Return a paginated list of uploads for a project, ordered by newest first."""
    qs: QuerySet[TestCaseUpload] = TestCaseUpload.objects.filter(
        project=project
    ).order_by("-created_at")
    paginator: Paginator[TestCaseUpload] = Paginator(qs, per_page)
    return paginator.get_page(page)


# ============================================================================
# XML PARSING SERVICES
# ============================================================================


def validate_testrail_xml(content: str) -> tuple[bool, str]:
    """Validate that content is a well-formed TestRail XML export.

    Returns (True, "") if valid, (False, error_message) if not.
    Checks: valid XML, has <suite> root, has at least one <case> element.
    """
    root = _parse_xml_root(content)
    if root is None:
        return False, "Content is not valid XML."

    if root.tag != "suite":
        return False, "Root element must be <suite>, not <{0}>.".format(root.tag)

    if not _has_case_elements(root):
        return False, "XML must contain at least one <case> element."

    return True, ""


def parse_testrail_xml(file_path: str) -> list[ParsedTestCase]:
    """Parse a TestRail XML export file into a list of ParsedTestCase dataclasses.

    Walks <suite>/<sections>/<section>/<cases>/<case> and extracts all fields.
    HTML entities in <custom> children are decoded via html.unescape().
    """
    tree = ET.parse(file_path)  # noqa: S314
    root = tree.getroot()
    cases: list[ParsedTestCase] = []

    for case_element in root.iter("case"):
        cases.append(_parse_single_case(case_element))

    return cases


@transaction.atomic
def bulk_create_test_cases_from_parsed(
    *,
    upload: TestCaseUpload,
    project: Project,
    parsed_cases: list[ParsedTestCase],
    batch_size: int = 50,
    progress_callback: Callable[[int], None] | None = None,
) -> int:
    """Batch-create TestCase objects from parsed data.

    Calls progress_callback with the cumulative processed count after each batch.
    Returns total created count.
    """
    total_created = 0

    for batch in _split_into_batches(parsed_cases, batch_size):
        objects = [_to_test_case(parsed, upload, project) for parsed in batch]
        TestCase.objects.bulk_create(objects)
        total_created += len(objects)
        if progress_callback is not None:
            progress_callback(total_created)

    return total_created


# ============================================================================
# XML PARSING HELPERS (private)
# ============================================================================


def _parse_xml_root(content: str) -> ET.Element | None:
    """Attempt to parse XML content and return root element, or None on failure."""
    try:
        return ET.fromstring(content)  # noqa: S314
    except ET.ParseError:
        return None


def _has_case_elements(root: ET.Element) -> bool:
    """Check whether the XML tree contains at least one <case> element."""
    return next(root.iter("case"), None) is not None


def _parse_single_case(case_element: ET.Element) -> ParsedTestCase:
    """Extract all fields from a single <case> XML element into a ParsedTestCase."""
    custom = case_element.find("custom")
    preconditions, steps, expected = _extract_custom_fields(custom)

    return ParsedTestCase(
        testrail_id=_text(case_element, "id"),
        title=_text(case_element, "title"),
        template=_text(case_element, "template") or "Test Case",
        type=_text(case_element, "type"),
        priority=_text(case_element, "priority"),
        estimate=_text(case_element, "estimate"),
        references=_text(case_element, "references"),
        preconditions=preconditions,
        steps=steps,
        expected=expected,
        is_converted=_text(case_element, "is_converted") == "1",
    )


def _extract_custom_fields(
    custom: ET.Element | None,
) -> tuple[str, str, str]:
    """Extract and HTML-decode preconditions, steps, and expected from <custom>."""
    if custom is None:
        return "", "", ""

    preconditions = _decoded_text(custom, "preconds")
    steps = _decoded_text(custom, "steps")
    expected = _decoded_text(custom, "expected")
    return preconditions, steps, expected


def _text(parent: ET.Element, tag: str) -> str:
    """Get text content of a child element, or empty string if missing."""
    child = parent.find(tag)
    if child is None or child.text is None:
        return ""
    return child.text


def _decoded_text(parent: ET.Element, tag: str) -> str:
    """Get HTML-decoded text content of a child element."""
    raw = _text(parent, tag)
    if not raw:
        return ""
    return html.unescape(raw)


def _to_test_case(
    parsed: ParsedTestCase, upload: TestCaseUpload, project: Project
) -> TestCase:
    """Convert a ParsedTestCase dataclass into an unsaved TestCase model instance."""
    return TestCase(
        project=project,
        upload=upload,
        testrail_id=parsed.testrail_id,
        title=parsed.title,
        template=parsed.template,
        type=parsed.type,
        priority=parsed.priority,
        estimate=parsed.estimate,
        references=parsed.references,
        preconditions=parsed.preconditions,
        steps=parsed.steps,
        expected=parsed.expected,
        is_converted=parsed.is_converted,
    )


def is_valid_xml_filename(filename: str) -> bool:
    return filename.lower().endswith(".xml")


def _split_into_batches(
    items: list[ParsedTestCase], batch_size: int
) -> list[list[ParsedTestCase]]:
    return [items[i : i + batch_size] for i in range(0, len(items), batch_size)]


# ============================================================================
# TEST RUN EXECUTION SERVICES
# ============================================================================


def execute_test_run_test_case(pivot_id: int) -> None:
    pivot = _fetch_pivot(pivot_id)
    _mark_pivot_in_progress(pivot)

    client = get_docker_client()
    container_id = ""
    try:
        container_info = provision_environment(client, name_suffix=f"trtc-{pivot_id}")
        container_id = container_info.container_id

        on_log = _build_log_callback(pivot)
        on_screenshot = _build_screenshot_callback(pivot)
        config = _build_config_with_callbacks(
            build_agent_config(), on_log, on_screenshot
        )
        task_description = _build_task_description(pivot.test_case)

        result = run_agent(task_description, container_info.ports, config=config)
        _finalize_pivot(pivot, result)
    except Exception as exc:
        logger.exception("execute_test_run_test_case failed for pivot %d", pivot_id)
        _mark_pivot_failed(pivot, str(exc))
    finally:
        if container_id:
            teardown_environment(client, container_id)
        close_docker_client(client)
        _update_test_run_status_if_needed(pivot.test_run)


def _fetch_pivot(pivot_id: int) -> TestRunTestCase:
    return TestRunTestCase.objects.select_related("test_run", "test_case").get(
        pk=pivot_id
    )


def _mark_pivot_in_progress(pivot: TestRunTestCase) -> None:
    pivot.status = TestRunTestCaseStatus.IN_PROGRESS
    pivot.save(update_fields=["status", "updated_at"])

    test_run = pivot.test_run
    if test_run.status == TestRunStatus.WAITING:
        test_run.status = TestRunStatus.STARTED
        test_run.save(update_fields=["status", "updated_at"])


def _build_log_callback(pivot: TestRunTestCase) -> Callable[[str], None]:
    def _append_log(message: str) -> None:
        pivot.logs += message + "\n"
        pivot.save(update_fields=["logs", "updated_at"])

    return _append_log


def _build_screenshot_callback(pivot: TestRunTestCase) -> ScreenshotCallback:
    def _save_screenshot(base64_data: str, tool_name: str) -> None:
        image_bytes = _decode_base64_image(base64_data)
        filename = _generate_screenshot_filename(tool_name)
        _persist_screenshot(pivot, image_bytes, filename, tool_name)

    return _save_screenshot


def _decode_base64_image(base64_data: str) -> bytes:
    return base64.b64decode(base64_data)


def _generate_screenshot_filename(tool_name: str) -> str:
    timestamp_ms = int(time.time() * 1000)
    return f"{timestamp_ms}_{tool_name}.png"


def _persist_screenshot(
    pivot: TestRunTestCase,
    image_bytes: bytes,
    filename: str,
    tool_name: str,
) -> None:
    TestRunScreenshot.objects.create(
        test_run_test_case=pivot,
        image=ContentFile(image_bytes, name=filename),
        tool_name=tool_name,
    )


def _build_config_with_callbacks(
    config: AgentConfig,
    on_log: Callable[[str], None],
    on_screenshot: ScreenshotCallback,
) -> AgentConfig:
    return dataclasses.replace(config, on_log=on_log, on_screenshot=on_screenshot)


def _build_task_description(test_case: TestCase) -> str:
    parts: list[str] = [f"Test Case: {test_case.title}"]
    if test_case.preconditions:
        parts.append(f"\nPreconditions:\n{test_case.preconditions}")
    if test_case.steps:
        parts.append(f"\nSteps:\n{test_case.steps}")
    if test_case.expected:
        parts.append(f"\nExpected Result:\n{test_case.expected}")
    return "\n".join(parts)


def _finalize_pivot(pivot: TestRunTestCase, result: AgentResult) -> None:
    if result.stop_reason == AgentStopReason.TASK_COMPLETE:
        pivot.status = TestRunTestCaseStatus.SUCCESS
    else:
        pivot.status = TestRunTestCaseStatus.FAILED

    pivot.result = _extract_agent_summary(result)
    pivot.save(update_fields=["status", "result", "updated_at"])


def _extract_agent_summary(result: AgentResult) -> str:
    for message in reversed(result.messages):
        if message.role == "assistant" and isinstance(message.content, str):
            return message.content
    return result.error or ""


def _mark_pivot_failed(pivot: TestRunTestCase, error: str) -> None:
    pivot.status = TestRunTestCaseStatus.FAILED
    pivot.result = error
    pivot.save(update_fields=["status", "result", "updated_at"])


def _update_test_run_status_if_needed(test_run: TestRun) -> None:
    test_run.refresh_from_db()
    all_pivots = test_run.pivot_entries.all()

    if not all_pivots.exists():
        return

    all_done = not all_pivots.filter(
        status__in=[TestRunTestCaseStatus.CREATED, TestRunTestCaseStatus.IN_PROGRESS]
    ).exists()

    if all_done:
        test_run.status = TestRunStatus.DONE
        test_run.save(update_fields=["status", "updated_at"])
