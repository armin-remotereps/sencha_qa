from __future__ import annotations

import logging
from typing import Any

from asgiref.sync import async_to_sync
from celery import shared_task
from celery.app.task import Task
from channels.layers import get_channel_layer
from django.db import transaction

from projects.models import Project, TestCaseUpload, TestRun, UploadStatus
from projects.prompt_refiner import refine_project_prompt
from projects.services import _agent_status_group, save_project_prompt
from projects.testrail_client import TestRailError
from projects.testrail_mapping import TestRailFieldMapper
from projects.testrail_results_services import (
    mark_testrail_push_failed,
    mark_testrail_push_succeeded,
)
from projects.testrail_results_services import (
    push_test_run_results as push_results_to_testrail,
)
from projects.testrail_services import (
    TestRailNotConfiguredError,
    build_testrail_client,
    upsert_test_cases_from_testrail,
)

logger: logging.Logger = logging.getLogger(__name__)

GENERIC_UPLOAD_FAILURE_MESSAGE = "An error occurred while processing the upload."
GENERIC_PUSH_FAILURE_MESSAGE = "An error occurred while pushing results to TestRail."


def _send_upload_progress(upload: TestCaseUpload) -> None:
    channel_layer: Any = get_channel_layer()
    if channel_layer is None:
        return

    async_to_sync(channel_layer.group_send)(
        f"upload_{upload.project_id}",
        {
            "type": "upload.progress",
            "upload_id": upload.id,
            "status": upload.status,
            "total_cases": upload.total_cases,
            "processed_cases": upload.processed_cases,
            "error_message": upload.error_message,
        },
    )


def _fetch_upload(upload_id: int) -> TestCaseUpload | None:
    try:
        return TestCaseUpload.objects.get(id=upload_id)
    except TestCaseUpload.DoesNotExist:
        return None


def _mark_processing(upload: TestCaseUpload) -> None:
    upload.status = UploadStatus.PROCESSING
    upload.save(update_fields=["status", "updated_at"])


def _process_upload_file(upload: TestCaseUpload) -> None:
    from projects.services import (
        bulk_create_test_cases_from_parsed,
        parse_testrail_xml,
    )

    parsed_cases = parse_testrail_xml(upload.file.path)

    upload.total_cases = len(parsed_cases)
    upload.save(update_fields=["total_cases", "updated_at"])
    _send_upload_progress(upload)

    def on_batch_processed(processed: int) -> None:
        upload.processed_cases = processed
        upload.save(update_fields=["processed_cases", "updated_at"])
        _send_upload_progress(upload)

    bulk_create_test_cases_from_parsed(
        upload=upload,
        project=upload.project,
        parsed_cases=parsed_cases,
        batch_size=50,
        progress_callback=on_batch_processed,
    )


def _mark_completed(upload: TestCaseUpload) -> None:
    upload.status = UploadStatus.COMPLETED
    upload.file.delete(save=False)
    upload.save(update_fields=["status", "updated_at"])
    _send_upload_progress(upload)


def _handle_failure(
    upload: TestCaseUpload, message: str = GENERIC_UPLOAD_FAILURE_MESSAGE
) -> None:
    upload.refresh_from_db()
    upload.status = UploadStatus.FAILED
    upload.error_message = message
    upload.save(update_fields=["status", "error_message", "updated_at"])

    with transaction.atomic():
        upload.test_cases.all().delete()

    _send_upload_progress(upload)


@shared_task(
    bind=True,
    name="projects.tasks.process_xml_upload",
    queue="upload",
    max_retries=0,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=600,
    time_limit=660,
)
def process_xml_upload(self: Task[[int], None], upload_id: int) -> None:
    logger.info(
        "process_xml_upload started: task_id=%s upload_id=%s",
        self.request.id,
        upload_id,
    )

    upload = _fetch_upload(upload_id)
    if upload is None:
        logger.error(
            "TestCaseUpload id=%s does not exist; aborting task_id=%s",
            upload_id,
            self.request.id,
        )
        return

    _mark_processing(upload)

    try:
        _process_upload_file(upload)
        _mark_completed(upload)
        logger.info(
            "process_xml_upload completed: task_id=%s upload_id=%s total_cases=%s",
            self.request.id,
            upload_id,
            upload.total_cases,
        )
    except Exception:
        logger.exception(
            "process_xml_upload failed: task_id=%s upload_id=%s",
            self.request.id,
            upload_id,
        )
        _handle_failure(upload)


def _run_testrail_import(upload: TestCaseUpload) -> None:
    if upload.testrail_project_id is None or upload.testrail_suite_id is None:
        raise TestRailError(
            "Import row is missing its TestRail project or suite id.", 0
        )

    with build_testrail_client(upload.project) as client:
        mapper = TestRailFieldMapper.from_vocabularies(
            client.get_case_types(), client.get_priorities()
        )
        cases = list(
            client.iter_cases(upload.testrail_project_id, upload.testrail_suite_id)
        )

    upload.total_cases = len(cases)
    upload.save(update_fields=["total_cases", "updated_at"])
    _send_upload_progress(upload)

    def on_batch_processed(processed: int, updated: int) -> None:
        upload.processed_cases = processed
        upload.updated_cases = updated
        upload.save(update_fields=["processed_cases", "updated_cases", "updated_at"])
        _send_upload_progress(upload)

    upsert_test_cases_from_testrail(
        upload=upload,
        project=upload.project,
        cases=cases,
        mapper=mapper,
        batch_size=50,
        progress_callback=on_batch_processed,
    )


@shared_task(
    bind=True,
    name="projects.tasks.import_testrail_cases",
    queue="upload",
    max_retries=0,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=600,
    time_limit=660,
)
def import_testrail_cases(self: Task[[int], None], upload_id: int) -> None:
    logger.info(
        "import_testrail_cases started: task_id=%s upload_id=%s",
        self.request.id,
        upload_id,
    )
    upload = _fetch_upload(upload_id)
    if upload is None:
        logger.error(
            "TestCaseUpload id=%s does not exist; aborting task_id=%s",
            upload_id,
            self.request.id,
        )
        return

    _mark_processing(upload)
    try:
        _run_testrail_import(upload)
        _mark_completed(upload)
        logger.info(
            "import_testrail_cases completed: task_id=%s upload_id=%s total=%s updated=%s",
            self.request.id,
            upload_id,
            upload.total_cases,
            upload.updated_cases,
        )
    except (TestRailError, TestRailNotConfiguredError) as exc:
        status_code = exc.status_code if isinstance(exc, TestRailError) else 0
        logger.warning(
            "import_testrail_cases failed (TestRail): task_id=%s upload_id=%s status=%s message=%s",
            self.request.id,
            upload_id,
            status_code,
            exc.message,
        )
        _handle_failure(upload, exc.message)
    except Exception:
        logger.exception(
            "import_testrail_cases failed: task_id=%s upload_id=%s",
            self.request.id,
            upload_id,
        )
        _handle_failure(upload)


@shared_task(
    bind=True,
    name="projects.tasks.execute_test_run_case",
    queue="execution",
    max_retries=0,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=1800,
    time_limit=1860,
)
def execute_test_run_case(self: Task[[int], None], pivot_id: int) -> None:
    from projects.services import execute_test_run_test_case

    logger.info(
        "execute_test_run_case started: task_id=%s pivot_id=%s",
        self.request.id,
        pivot_id,
    )
    execute_test_run_test_case(pivot_id)
    logger.info(
        "execute_test_run_case finished: task_id=%s pivot_id=%s",
        self.request.id,
        pivot_id,
    )


def _send_prompt_refined(
    project_id: int,
    *,
    refined_prompt: str = "",
    error: str = "",
) -> None:
    channel_layer: Any = get_channel_layer()
    if channel_layer is None:
        return

    async_to_sync(channel_layer.group_send)(
        _agent_status_group(project_id),
        {
            "type": "prompt.refined",
            "refined_prompt": refined_prompt,
            "error": error,
        },
    )


@shared_task(
    bind=True,
    name="projects.tasks.refine_project_prompt_task",
    max_retries=0,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=660,
    time_limit=720,
)
def refine_project_prompt_task(
    self: Task[[int, str], None],
    project_id: int,
    raw_prompt: str,
) -> None:
    logger.info(
        "refine_project_prompt_task started: task_id=%s project_id=%s",
        self.request.id,
        project_id,
    )
    try:
        refined = refine_project_prompt(raw_prompt)
        project = Project.objects.get(id=project_id)
        save_project_prompt(project=project, prompt=refined)
        _send_prompt_refined(project_id, refined_prompt=refined)
        logger.info(
            "refine_project_prompt_task completed: task_id=%s project_id=%s",
            self.request.id,
            project_id,
        )
    except Exception:
        logger.exception(
            "refine_project_prompt_task failed: task_id=%s project_id=%s",
            self.request.id,
            project_id,
        )
        _send_prompt_refined(project_id, error="Refinement service unavailable.")


def _fetch_test_run(test_run_id: int) -> TestRun | None:
    try:
        return TestRun.objects.get(id=test_run_id)
    except TestRun.DoesNotExist:
        return None


@shared_task(
    bind=True,
    name="projects.tasks.push_test_run_results",
    queue="upload",
    max_retries=0,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=600,
    time_limit=660,
)
def push_test_run_results(
    self: Task[[int, str], None], test_run_id: int, links_base_url: str
) -> None:
    logger.info(
        "push_test_run_results started: task_id=%s test_run_id=%s",
        self.request.id,
        test_run_id,
    )

    test_run = _fetch_test_run(test_run_id)
    if test_run is None:
        logger.error(
            "TestRun id=%s does not exist; aborting task_id=%s",
            test_run_id,
            self.request.id,
        )
        return

    try:
        outcome = push_results_to_testrail(test_run, links_base_url=links_base_url)
        mark_testrail_push_succeeded(test_run, outcome)
        logger.info(
            "push_test_run_results completed: task_id=%s test_run_id=%s "
            "pushed=%s unchanged=%s skipped_no_case_id=%s testrail_run_id=%s",
            self.request.id,
            test_run_id,
            outcome.pushed,
            outcome.unchanged,
            outcome.skipped_no_case_id,
            outcome.testrail_run_id,
        )
    except (TestRailError, TestRailNotConfiguredError) as exc:
        status_code = exc.status_code if isinstance(exc, TestRailError) else 0
        logger.warning(
            "push_test_run_results failed (TestRail): task_id=%s test_run_id=%s status=%s message=%s",
            self.request.id,
            test_run_id,
            status_code,
            exc.message,
        )
        mark_testrail_push_failed(test_run, exc.message)
    except Exception:
        logger.exception(
            "push_test_run_results failed: task_id=%s test_run_id=%s",
            self.request.id,
            test_run_id,
        )
        mark_testrail_push_failed(test_run, GENERIC_PUSH_FAILURE_MESSAGE)
