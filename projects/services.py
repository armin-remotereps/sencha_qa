from __future__ import annotations

import asyncio
import base64
import dataclasses
import html
import io
import logging
import secrets
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, TypedDict

from accounts.models import CustomUser
from agents.types import (
    AgentConfig,
    AgentResult,
    AgentStopReason,
    ChatMessage,
    ScreenshotCallback,
)
from asgiref.sync import async_to_sync
from auto_tester.celery import app as celery_app
from celery import chain
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import UploadedFile
from django.core.paginator import Page, Paginator
from django.db import transaction
from django.db.models import Count, Q, QuerySet
from django.utils import timezone
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

_AGENT_POLL_INTERVAL_SECONDS = 2
_INTERACTIVE_TERMINATE_TIMEOUT_SECONDS = 30.0


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


def _clone_test_case(*, source: TestCase, target_project: Project) -> TestCase:
    return TestCase(
        project=target_project,
        upload=None,
        testrail_id=source.testrail_id,
        title=source.title,
        template=source.template,
        type=source.type,
        priority=source.priority,
        estimate=source.estimate,
        references=source.references,
        preconditions=source.preconditions,
        steps=source.steps,
        expected=source.expected,
        is_converted=source.is_converted,
    )


@transaction.atomic
def duplicate_project(
    *, source_project: Project, user: CustomUser, name: str
) -> Project:
    """Copies tags and test cases (without upload association) from the source."""
    new_project = Project.objects.create(name=name)
    _sync_tags(new_project, list(source_project.tags.values_list("name", flat=True)))
    new_project.members.add(user)
    source_cases = TestCase.objects.filter(project=source_project)
    copies = [
        _clone_test_case(source=tc, target_project=new_project) for tc in source_cases
    ]
    TestCase.objects.bulk_create(copies)
    return new_project


@transaction.atomic
def copy_test_cases_to_project(
    *, source_project: Project, target_project: Project, test_case_ids: list[int]
) -> int:
    """Only test cases belonging to source_project are copied. Returns the count."""
    source_cases = TestCase.objects.filter(id__in=test_case_ids, project=source_project)
    copies = [
        _clone_test_case(source=tc, target_project=target_project)
        for tc in source_cases
    ]
    TestCase.objects.bulk_create(copies)
    return len(copies)


def list_other_projects_for_user(
    *, user: CustomUser, exclude_project: Project
) -> QuerySet[Project]:
    """Results are ordered alphabetically by name."""
    return (
        Project.objects.filter(members=user, archived=False)
        .exclude(id=exclude_project.id)
        .order_by("name")
    )


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
# CONTROLLER AGENT SERVICES
# ============================================================================


def generate_api_key() -> str:
    return secrets.token_urlsafe(32)


def get_project_by_api_key(api_key: str) -> Project | None:
    try:
        return Project.objects.filter(api_key=api_key, archived=False).get()
    except Project.DoesNotExist:
        return None


def regenerate_api_key(project: Project) -> str:
    project.api_key = generate_api_key()
    project.save()
    return project.api_key


def mark_agent_connected(project: Project, system_info: dict[str, Any]) -> bool:
    """Returns True if connection established, False if already connected."""
    rows_updated = Project.objects.filter(id=project.id, agent_connected=False).update(
        agent_connected=True,
        agent_system_info=system_info,
    )
    if rows_updated > 0:
        project.refresh_from_db()
        return True
    return False


def mark_agent_disconnected(project: Project) -> None:
    project.agent_connected = False
    project.agent_system_info = {}
    project.last_connected_at = timezone.now()
    project.save()


class AgentStatusEvent(TypedDict):
    type: str
    agent_connected: bool
    agent_system_info: dict[str, Any]
    last_connected_at: str | None


def _agent_status_group(project_id: int) -> str:
    return f"agent_status_{project_id}"


def _build_agent_status_event(project: Project) -> AgentStatusEvent:
    return {
        "type": "agent.status",
        "agent_connected": project.agent_connected,
        "agent_system_info": project.agent_system_info,
        "last_connected_at": (
            project.last_connected_at.isoformat() if project.last_connected_at else None
        ),
    }


def broadcast_agent_status(project: Project) -> None:
    layer: Any = get_channel_layer()
    if layer is None:
        return

    event = _build_agent_status_event(project)
    async_to_sync(layer.group_send)(_agent_status_group(project.id), event)


# ============================================================================
# CONTROLLER ACTION SERVICES
# ============================================================================


class ControllerActionError(Exception):
    pass


class ActionResult(TypedDict):
    success: bool
    message: str
    duration_ms: float


class ScreenshotResult(TypedDict):
    success: bool
    image_base64: str
    width: int
    height: int
    format: str


class CommandResult(TypedDict):
    success: bool
    stdout: str
    stderr: str
    return_code: int
    duration_ms: float


def _controller_group(project_id: int) -> str:
    return f"controller_{project_id}"


def _get_channel_layer_or_raise() -> Any:
    layer: Any = get_channel_layer()
    if layer is None:
        raise ControllerActionError("Channel layer is not configured")
    return layer


def _dispatch_controller_action(
    project_id: int,
    event_type: str,
    reply_timeout: float,
    **payload: Any,
) -> dict[str, Any]:
    layer = _get_channel_layer_or_raise()
    reply_channel: str = async_to_sync(layer.new_channel)()
    request_id = str(uuid.uuid4())

    event: dict[str, Any] = {
        "type": event_type,
        "request_id": request_id,
        "reply_channel": reply_channel,
        **payload,
    }
    async_to_sync(layer.group_send)(_controller_group(project_id), event)

    try:
        result: dict[str, Any] = async_to_sync(asyncio.wait_for)(
            layer.receive(reply_channel), timeout=reply_timeout
        )
        return result
    except asyncio.TimeoutError as exc:
        raise ControllerActionError(
            f"Timed out waiting for reply after {reply_timeout}s"
        ) from exc


def _build_action_result(reply: dict[str, Any]) -> ActionResult:
    return ActionResult(
        success=reply.get("success", False),
        message=reply.get("message", ""),
        duration_ms=reply.get("duration_ms", 0.0),
    )


def controller_click(
    project_id: int,
    x: int,
    y: int,
    button: str = "left",
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id, "controller.click", timeout, x=x, y=y, button=button
    )
    return _build_action_result(reply)


def controller_hover(
    project_id: int,
    x: int,
    y: int,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id, "controller.hover", timeout, x=x, y=y
    )
    return _build_action_result(reply)


def controller_drag(
    project_id: int,
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    button: str = "left",
    duration: float = 0.5,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.drag",
        timeout,
        start_x=start_x,
        start_y=start_y,
        end_x=end_x,
        end_y=end_y,
        button=button,
        duration=duration,
    )
    return _build_action_result(reply)


def controller_type_text(
    project_id: int,
    text: str,
    interval: float = 0.0,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id, "controller.type_text", timeout, text=text, interval=interval
    )
    return _build_action_result(reply)


def controller_key_press(
    project_id: int,
    keys: str,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id, "controller.key_press", timeout, keys=keys
    )
    return _build_action_result(reply)


def controller_screenshot(
    project_id: int,
    timeout: float = 30.0,
) -> ScreenshotResult:
    reply = _dispatch_controller_action(project_id, "controller.screenshot", timeout)
    return ScreenshotResult(
        success=reply.get("success", False),
        image_base64=reply.get("image_base64", ""),
        width=reply.get("width", 0),
        height=reply.get("height", 0),
        format=reply.get("format", "png"),
    )


def controller_run_command(
    project_id: int,
    command: str,
) -> CommandResult:
    return controller_run_command_streaming(project_id, command)


def controller_run_command_streaming(
    project_id: int,
    command: str,
    on_output: Callable[[str, str], None] | None = None,
) -> CommandResult:
    layer = _get_channel_layer_or_raise()
    reply_channel: str = async_to_sync(layer.new_channel)()
    request_id = str(uuid.uuid4())
    timeout = float(settings.AGENT_TIMEOUT_SECONDS)

    _dispatch_run_command_event(layer, project_id, request_id, reply_channel, command)
    return _receive_streaming_command_result(layer, reply_channel, timeout, on_output)


def _dispatch_run_command_event(
    layer: Any,
    project_id: int,
    request_id: str,
    reply_channel: str,
    command: str,
) -> None:
    event: dict[str, Any] = {
        "type": "controller.run_command",
        "request_id": request_id,
        "reply_channel": reply_channel,
        "command": command,
    }
    async_to_sync(layer.group_send)(_controller_group(project_id), event)


def _receive_streaming_command_result(
    layer: Any,
    reply_channel: str,
    timeout: float,
    on_output: Callable[[str, str], None] | None,
) -> CommandResult:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ControllerActionError(
                f"Timed out waiting for command result after {timeout}s"
            )

        try:
            reply: dict[str, Any] = async_to_sync(asyncio.wait_for)(
                layer.receive(reply_channel), timeout=remaining
            )
        except asyncio.TimeoutError as exc:
            raise ControllerActionError(
                f"Timed out waiting for command result after {timeout}s"
            ) from exc

        msg_type = reply.get("type", "")
        if msg_type == "command.output":
            if on_output is not None:
                line = str(reply.get("line", ""))
                stream = str(reply.get("stream", "stdout"))
                on_output(line, stream)
            continue

        if msg_type == "command.result":
            return _build_command_result(reply)

        logger.warning("Unexpected message type in streaming receive: %s", msg_type)


def _build_command_result(reply: dict[str, Any]) -> CommandResult:
    return CommandResult(
        success=reply.get("success", False),
        stdout=reply.get("stdout", ""),
        stderr=reply.get("stderr", ""),
        return_code=reply.get("return_code", -1),
        duration_ms=reply.get("duration_ms", 0.0),
    )


# ============================================================================
# INTERACTIVE COMMAND SERVICES
# ============================================================================


class InteractiveCommandResult(TypedDict):
    session_id: str
    output: str
    is_alive: bool
    exit_code: int | None
    duration_ms: float


def _build_interactive_command_result(
    reply: dict[str, Any],
) -> InteractiveCommandResult:
    return InteractiveCommandResult(
        session_id=reply.get("session_id", ""),
        output=reply.get("output", ""),
        is_alive=reply.get("is_alive", False),
        exit_code=reply.get("exit_code"),
        duration_ms=reply.get("duration_ms", 0.0),
    )


def controller_start_interactive_command(
    project_id: int,
    command: str,
) -> InteractiveCommandResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.start_interactive_cmd",
        float(settings.INTERACTIVE_CMD_TIMEOUT_SECONDS),
        command=command,
    )
    return _build_interactive_command_result(reply)


def controller_send_input(
    project_id: int,
    session_id: str,
    input_text: str,
) -> InteractiveCommandResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.send_input",
        float(settings.INTERACTIVE_CMD_TIMEOUT_SECONDS),
        session_id=session_id,
        input_text=input_text,
    )
    return _build_interactive_command_result(reply)


def controller_terminate_interactive_command(
    project_id: int,
    session_id: str,
) -> InteractiveCommandResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.terminate_interactive_cmd",
        _INTERACTIVE_TERMINATE_TIMEOUT_SECONDS,
        session_id=session_id,
    )
    return _build_interactive_command_result(reply)


# ============================================================================
# BROWSER ACTION SERVICES
# ============================================================================


class BrowserContentResult(TypedDict):
    success: bool
    content: str
    duration_ms: float


def _build_browser_content_result(reply: dict[str, Any]) -> BrowserContentResult:
    return BrowserContentResult(
        success=reply.get("success", False),
        content=reply.get("content", ""),
        duration_ms=reply.get("duration_ms", 0.0),
    )


def controller_browser_navigate(
    project_id: int,
    url: str,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id, "controller.browser_navigate", timeout, url=url
    )
    return _build_action_result(reply)


def controller_browser_click(
    project_id: int,
    element_index: int,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.browser_click",
        timeout,
        element_index=element_index,
    )
    return _build_action_result(reply)


def controller_browser_type(
    project_id: int,
    element_index: int,
    text: str,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.browser_type",
        timeout,
        element_index=element_index,
        text=text,
    )
    return _build_action_result(reply)


def controller_browser_hover(
    project_id: int,
    element_index: int,
    timeout: float = 30.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id,
        "controller.browser_hover",
        timeout,
        element_index=element_index,
    )
    return _build_action_result(reply)


def controller_browser_get_elements(
    project_id: int,
    timeout: float = 30.0,
) -> BrowserContentResult:
    reply = _dispatch_controller_action(
        project_id, "controller.browser_get_elements", timeout
    )
    return _build_browser_content_result(reply)


def controller_browser_get_page_content(
    project_id: int,
    timeout: float = 30.0,
) -> BrowserContentResult:
    reply = _dispatch_controller_action(
        project_id, "controller.browser_get_page_content", timeout
    )
    return _build_browser_content_result(reply)


def controller_browser_get_url(
    project_id: int,
    timeout: float = 30.0,
) -> BrowserContentResult:
    reply = _dispatch_controller_action(
        project_id, "controller.browser_get_url", timeout
    )
    return _build_browser_content_result(reply)


def controller_browser_download(
    project_id: int,
    url: str,
    save_path: str = "",
    timeout: float = 120.0,
) -> ActionResult:
    reply = _dispatch_controller_action(
        project_id, "controller.browser_download", timeout, url=url, save_path=save_path
    )
    return _build_action_result(reply)


def controller_browser_take_screenshot(
    project_id: int,
    timeout: float = 30.0,
) -> ScreenshotResult:
    reply = _dispatch_controller_action(
        project_id, "controller.browser_take_screenshot", timeout
    )
    return ScreenshotResult(
        success=reply.get("success", False),
        image_base64=reply.get("image_base64", ""),
        width=reply.get("width", 0),
        height=reply.get("height", 0),
        format=reply.get("format", "png"),
    )


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
# TEST RUN UI SERVICES
# ============================================================================


@transaction.atomic
def create_test_run_with_cases(
    *, project: Project, test_case_ids: list[int]
) -> TestRun:
    test_run = TestRun.objects.create(project=project)
    valid_cases = TestCase.objects.filter(id__in=test_case_ids, project=project)
    pivots = [TestRunTestCase(test_run=test_run, test_case=tc) for tc in valid_cases]
    TestRunTestCase.objects.bulk_create(pivots)
    return test_run


@transaction.atomic
def add_cases_to_test_run(*, test_run: TestRun, test_case_ids: list[int]) -> int:
    valid_cases = TestCase.objects.filter(
        id__in=test_case_ids, project=test_run.project
    )
    existing_case_ids = set(
        test_run.pivot_entries.values_list("test_case_id", flat=True)
    )
    new_pivots = [
        TestRunTestCase(test_run=test_run, test_case=tc)
        for tc in valid_cases
        if tc.id not in existing_case_ids
    ]
    TestRunTestCase.objects.bulk_create(new_pivots)
    return len(new_pivots)


def list_test_runs_for_project(
    *, project: Project, page: int, per_page: int
) -> Page[TestRun]:
    qs = (
        TestRun.objects.filter(project=project)
        .annotate(case_count=Count("pivot_entries"))
        .order_by("-created_at")
    )
    paginator: Paginator[TestRun] = Paginator(qs, per_page)
    return paginator.get_page(page)


def get_test_run_for_project(test_run_id: int, project: Project) -> TestRun | None:
    try:
        return TestRun.objects.filter(id=test_run_id, project=project).get()
    except TestRun.DoesNotExist:
        return None


def get_test_run_case_detail(pivot_id: int, project: Project) -> TestRunTestCase | None:
    try:
        return (
            TestRunTestCase.objects.select_related("test_run", "test_case")
            .filter(id=pivot_id, test_run__project=project)
            .get()
        )
    except TestRunTestCase.DoesNotExist:
        return None


def list_test_run_cases(
    *, test_run: TestRun, page: int, per_page: int
) -> Page[TestRunTestCase]:
    qs = test_run.pivot_entries.select_related("test_case").order_by("-created_at")
    paginator: Paginator[TestRunTestCase] = Paginator(qs, per_page)
    return paginator.get_page(page)


def list_waiting_test_runs_for_project(project: Project) -> QuerySet[TestRun]:
    return TestRun.objects.filter(
        project=project, status=TestRunStatus.WAITING
    ).order_by("-created_at")


def _can_modify_test_run(test_run: TestRun) -> bool:
    return test_run.status == TestRunStatus.WAITING


def delete_test_run(test_run: TestRun) -> None:
    if not _can_modify_test_run(test_run):
        raise ValueError("Only waiting test runs can be deleted.")
    test_run.delete()


def remove_case_from_test_run(test_run: TestRun, pivot_id: int) -> None:
    if not _can_modify_test_run(test_run):
        raise ValueError("Cannot remove cases from a non-waiting test run.")
    test_run.pivot_entries.filter(id=pivot_id).delete()


@transaction.atomic
def redo_test_run(test_run: TestRun) -> None:
    if test_run.status not in (TestRunStatus.DONE, TestRunStatus.CANCELLED):
        raise ValueError("Only completed or cancelled test runs can be redone.")
    for pivot in test_run.pivot_entries.all():
        for screenshot in pivot.screenshots.all():
            screenshot.image.delete(save=False)
        pivot.screenshots.all().delete()
        pivot.status = TestRunTestCaseStatus.CREATED
        pivot.result = ""
        pivot.logs = ""
        pivot.save(update_fields=["status", "result", "logs", "updated_at"])
    test_run.status = TestRunStatus.WAITING
    test_run.celery_task_id = ""
    test_run.save(update_fields=["status", "celery_task_id", "updated_at"])


def _has_active_test_run(project: Project) -> bool:
    return TestRun.objects.filter(
        project=project, status=TestRunStatus.STARTED
    ).exists()


def start_test_run(test_run: TestRun) -> None:
    from projects.tasks import execute_test_run_case

    pivot_ids = list(test_run.pivot_entries.values_list("id", flat=True))
    if not pivot_ids:
        raise ValueError("Cannot start a test run with no test cases.")
    if test_run.status != TestRunStatus.WAITING:
        raise ValueError("Test run is not in WAITING status.")
    if _has_active_test_run(test_run.project):
        raise ValueError("Another test run is already in progress for this project.")

    test_run.status = TestRunStatus.STARTED
    test_run.save(update_fields=["status", "updated_at"])
    _broadcast_test_run_status(test_run)

    result = chain(*[execute_test_run_case.si(pid) for pid in pivot_ids]).apply_async()
    test_run.celery_task_id = result.id
    test_run.save(update_fields=["celery_task_id", "updated_at"])


def get_test_run_summary(test_run: TestRun) -> dict[str, int]:
    result = test_run.pivot_entries.aggregate(
        total=Count("id"),
        created=Count("id", filter=Q(status=TestRunTestCaseStatus.CREATED)),
        in_progress=Count("id", filter=Q(status=TestRunTestCaseStatus.IN_PROGRESS)),
        success=Count("id", filter=Q(status=TestRunTestCaseStatus.SUCCESS)),
        failed=Count("id", filter=Q(status=TestRunTestCaseStatus.FAILED)),
        cancelled=Count("id", filter=Q(status=TestRunTestCaseStatus.CANCELLED)),
    )
    return {k: v or 0 for k, v in result.items()}


# ============================================================================
# BROADCAST HELPERS
# ============================================================================


def _test_run_group(test_run_id: int) -> str:
    return f"test_run_{test_run_id}"


def _test_run_case_group(pivot_id: int) -> str:
    return f"test_run_case_{pivot_id}"


class PivotStatusEvent(TypedDict):
    type: str
    pivot_id: int
    status: str
    summary: dict[str, int]


class CaseStatusEvent(TypedDict):
    type: str
    status: str
    result: str


class TestRunStatusEvent(TypedDict):
    type: str
    test_run_status: str
    summary: dict[str, int]


class CaseLogEvent(TypedDict):
    type: str
    message: str


class CaseScreenshotEvent(TypedDict):
    type: str
    screenshot_id: int
    image_url: str
    tool_name: str
    created_at: str


def _broadcast_pivot_status_to_run(pivot: TestRunTestCase) -> None:
    layer: Any = get_channel_layer()
    if layer is None:
        return

    event: PivotStatusEvent = {
        "type": "test_run.pivot_status",
        "pivot_id": pivot.id,
        "status": pivot.status,
        "summary": get_test_run_summary(pivot.test_run),
    }
    async_to_sync(layer.group_send)(_test_run_group(pivot.test_run.id), event)


def _broadcast_pivot_status_to_case(pivot: TestRunTestCase) -> None:
    layer: Any = get_channel_layer()
    if layer is None:
        return

    event: CaseStatusEvent = {
        "type": "test_run_case.status",
        "status": pivot.status,
        "result": pivot.result,
    }
    async_to_sync(layer.group_send)(_test_run_case_group(pivot.id), event)


def _broadcast_test_run_status(test_run: TestRun) -> None:
    layer: Any = get_channel_layer()
    if layer is None:
        return

    event: TestRunStatusEvent = {
        "type": "test_run.status",
        "test_run_status": test_run.status,
        "summary": get_test_run_summary(test_run),
    }
    async_to_sync(layer.group_send)(_test_run_group(test_run.id), event)


def _broadcast_log(pivot: TestRunTestCase, message: str) -> None:
    layer: Any = get_channel_layer()
    if layer is None:
        return

    event: CaseLogEvent = {
        "type": "test_run_case.log",
        "message": message,
    }
    async_to_sync(layer.group_send)(_test_run_case_group(pivot.id), event)


def _broadcast_screenshot(
    pivot: TestRunTestCase, screenshot: TestRunScreenshot
) -> None:
    layer: Any = get_channel_layer()
    if layer is None:
        return

    event: CaseScreenshotEvent = {
        "type": "test_run_case.screenshot",
        "screenshot_id": screenshot.id,
        "image_url": screenshot.image.url,
        "tool_name": screenshot.tool_name,
        "created_at": screenshot.created_at.isoformat(),
    }
    async_to_sync(layer.group_send)(_test_run_case_group(pivot.id), event)


def fetch_test_run_state(
    test_run_id: int,
) -> tuple[TestRun, dict[str, int], list[tuple[int, str]]]:
    test_run = TestRun.objects.get(pk=test_run_id)
    summary = get_test_run_summary(test_run)
    pivot_data: list[tuple[int, str]] = list(
        test_run.pivot_entries.values_list("id", "status")
    )
    return test_run, summary, pivot_data


def fetch_test_case_state(
    pivot_id: int,
) -> tuple[TestRunTestCase, list[TestRunScreenshot]]:
    pivot = TestRunTestCase.objects.select_related("test_case").get(pk=pivot_id)
    screenshots: list[TestRunScreenshot] = list(
        pivot.screenshots.all().order_by("created_at")
    )
    return pivot, screenshots


# ============================================================================
# TEST RUN EXECUTION SERVICES
# ============================================================================


def _wait_for_agent_connection(
    project: Project,
    timeout: int | None = None,
) -> None:
    if timeout is None:
        timeout = settings.CONTROLLER_AGENT_CONNECT_TIMEOUT
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        project.refresh_from_db()
        if project.agent_connected:
            return
        time.sleep(_AGENT_POLL_INTERVAL_SECONDS)
    raise TimeoutError(
        f"Controller agent did not connect within {timeout}s for project {project.id}"
    )


def execute_test_run_test_case(pivot_id: int) -> None:
    from agents.services.agent_loop import build_agent_config, run_agent

    pivot = _fetch_pivot(pivot_id)
    if pivot.test_run.status == TestRunStatus.CANCELLED:
        return

    project = pivot.test_run.project
    _mark_pivot_in_progress(pivot)

    try:
        if not project.agent_connected:
            _wait_for_agent_connection(project)

        on_log = _build_log_callback(pivot)
        on_screenshot = _build_screenshot_callback(pivot)
        config = _build_config_with_callbacks(
            build_agent_config(), on_log, on_screenshot
        )
        task_description = _build_task_description(pivot.test_case)

        result = run_agent(
            task_description,
            project.id,
            config=config,
            system_info=project.agent_system_info or None,
        )
        _finalize_pivot(pivot, result)
    except Exception as exc:
        logger.exception("execute_test_run_test_case failed for pivot %d", pivot_id)
        _mark_pivot_failed(pivot, str(exc))
        raise
    except BaseException as exc:
        logger.critical(
            "execute_test_run_test_case hit BaseException for pivot %d: %s",
            pivot_id,
            exc,
        )
        _mark_pivot_failed(pivot, str(exc))
        raise
    finally:
        _update_test_run_status_if_needed(pivot.test_run)


def _fetch_pivot(pivot_id: int) -> TestRunTestCase:
    return TestRunTestCase.objects.select_related("test_run__project", "test_case").get(
        pk=pivot_id
    )


def _mark_pivot_in_progress(pivot: TestRunTestCase) -> None:
    pivot.status = TestRunTestCaseStatus.IN_PROGRESS
    pivot.save(update_fields=["status", "updated_at"])

    test_run = pivot.test_run
    status_changed = False
    if test_run.status == TestRunStatus.WAITING:
        test_run.status = TestRunStatus.STARTED
        test_run.save(update_fields=["status", "updated_at"])
        status_changed = True

    _broadcast_pivot_status_to_run(pivot)
    _broadcast_pivot_status_to_case(pivot)
    if status_changed:
        _broadcast_test_run_status(test_run)


def _build_log_callback(pivot: TestRunTestCase) -> Callable[[str], None]:
    def _append_log(message: str) -> None:
        pivot.logs += message + "\n"
        pivot.save(update_fields=["logs", "updated_at"])
        _broadcast_log(pivot, message)

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
    screenshot = TestRunScreenshot.objects.create(
        test_run_test_case=pivot,
        image=ContentFile(image_bytes, name=filename),
        tool_name=tool_name,
    )
    _broadcast_screenshot(pivot, screenshot)


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
    _broadcast_pivot_status_to_run(pivot)
    _broadcast_pivot_status_to_case(pivot)


def _extract_agent_summary(result: AgentResult) -> str:
    for message in reversed(result.messages):
        if message.role == "assistant" and isinstance(message.content, str):
            return message.content
    return result.error or ""


def _mark_pivot_failed(pivot: TestRunTestCase, error: str) -> None:
    pivot.status = TestRunTestCaseStatus.FAILED
    pivot.result = error
    pivot.save(update_fields=["status", "result", "updated_at"])
    _broadcast_pivot_status_to_run(pivot)
    _broadcast_pivot_status_to_case(pivot)


def _mark_pivot_cancelled(pivot: TestRunTestCase) -> None:
    pivot.status = TestRunTestCaseStatus.CANCELLED
    pivot.save(update_fields=["status", "updated_at"])
    _broadcast_pivot_status_to_run(pivot)
    _broadcast_pivot_status_to_case(pivot)


@transaction.atomic
def abort_test_run(test_run: TestRun, reason: str = "Test run aborted") -> None:
    if test_run.status != TestRunStatus.STARTED:
        return

    test_run.status = TestRunStatus.CANCELLED
    test_run.save(update_fields=["status", "updated_at"])

    for pivot in test_run.pivot_entries.filter(
        status=TestRunTestCaseStatus.IN_PROGRESS
    ):
        _mark_pivot_failed(pivot, reason)

    for pivot in test_run.pivot_entries.filter(status=TestRunTestCaseStatus.CREATED):
        _mark_pivot_cancelled(pivot)

    _broadcast_test_run_status(test_run)

    celery_task_id = test_run.celery_task_id
    if celery_task_id:
        transaction.on_commit(
            lambda: celery_app.control.revoke(celery_task_id, terminate=True)
        )


def abort_active_test_run_on_disconnect(project: Project) -> None:
    active_run = TestRun.objects.filter(
        project=project, status=TestRunStatus.STARTED
    ).first()
    if active_run is not None:
        abort_test_run(active_run, reason="Controller client disconnected")


def _update_test_run_status_if_needed(test_run: TestRun) -> None:
    test_run.refresh_from_db()
    if test_run.status == TestRunStatus.CANCELLED:
        return

    all_pivots = test_run.pivot_entries.all()

    if not all_pivots.exists():
        return

    all_done = not all_pivots.filter(
        status__in=[TestRunTestCaseStatus.CREATED, TestRunTestCaseStatus.IN_PROGRESS]
    ).exists()

    if all_done:
        test_run.status = TestRunStatus.DONE
        test_run.save(update_fields=["status", "updated_at"])
        _broadcast_test_run_status(test_run)


# ============================================================================
# CONTROLLER CLIENT DOWNLOAD SERVICES
# ============================================================================

_CONTROLLER_CLIENT_EXCLUDE_DIRS = {".venv", "__pycache__", ".pytest_cache", "tests"}
_CONTROLLER_CLIENT_EXCLUDE_FILES = {".env"}


def _should_include_path(relative_path: Path) -> bool:
    for part in relative_path.parts:
        if part in _CONTROLLER_CLIENT_EXCLUDE_DIRS:
            return False
    if relative_path.name in _CONTROLLER_CLIENT_EXCLUDE_FILES:
        return False
    return True


def _generate_env_content(project: Project) -> str:
    return (
        f"CONTROLLER_HOST={settings.CONTROLLER_SERVER_HOST}\n"
        f"CONTROLLER_PORT={settings.CONTROLLER_SERVER_PORT}\n"
        f"CONTROLLER_API_KEY={project.api_key}\n"
        "CONTROLLER_RECONNECT_INTERVAL=5\n"
        "CONTROLLER_MAX_RECONNECT_ATTEMPTS=10\n"
        "CONTROLLER_LOG_LEVEL=INFO\n"
    )


def generate_controller_client_zip(project: Project) -> bytes:
    controller_dir = settings.BASE_DIR / "controller_client"
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(controller_dir.rglob("*")):
            if not file_path.is_file():
                continue
            relative = file_path.relative_to(controller_dir)
            if not _should_include_path(relative):
                continue
            arcname = str(Path("controller_client") / relative)
            zf.write(file_path, arcname)

        env_content = _generate_env_content(project)
        zf.writestr("controller_client/.env", env_content)

    return buffer.getvalue()
