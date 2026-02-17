from __future__ import annotations

import logging
from typing import Any

from asgiref.sync import async_to_sync
from celery import shared_task
from celery.app.task import Task
from channels.layers import get_channel_layer
from django.db import transaction

from projects.models import TestCaseUpload, UploadStatus

logger: logging.Logger = logging.getLogger(__name__)


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


def _handle_failure(upload: TestCaseUpload) -> None:
    upload.refresh_from_db()
    upload.status = UploadStatus.FAILED
    upload.error_message = "An error occurred while processing the upload."
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
