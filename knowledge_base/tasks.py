from __future__ import annotations

import logging

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings

from knowledge_base.services.copy_project_knowledge import (
    copy_project_knowledge as copy_project_knowledge_service,
)
from knowledge_base.services.expand_knowledge_archive import (
    expand_knowledge_archive as expand_knowledge_archive_service,
)
from knowledge_base.services.extract_knowledge_item import (
    extract_knowledge_item as extract_knowledge_item_service,
)
from knowledge_base.services.item_processing import (
    COPY_PROJECT_KNOWLEDGE_TASK,
    EXPAND_KNOWLEDGE_ARCHIVE_TASK,
    EXTRACT_KNOWLEDGE_ITEM_TASK,
    SWEEP_KNOWLEDGE_STORAGE_TASK,
)
from knowledge_base.services.sweep_knowledge_storage import (
    sweep_knowledge_storage as sweep_knowledge_storage_service,
)

logger = logging.getLogger(__name__)

# Extraction gets the configured limit; the hard kill comes 30 s later so the
# service can fail the item under its claim first.
_EXTRACT_SOFT_TIME_LIMIT: int = settings.KNOWLEDGE_BASE_EXTRACT_TIME_LIMIT_SECONDS
_EXTRACT_TIME_LIMIT = _EXTRACT_SOFT_TIME_LIMIT + 30
# Copy and sweep are set-based SQL and a directory walk.
_COPY_SOFT_TIME_LIMIT = 600
_COPY_TIME_LIMIT = 630
_SWEEP_SOFT_TIME_LIMIT = 1500
_SWEEP_TIME_LIMIT = 1530


@shared_task(
    name=EXTRACT_KNOWLEDGE_ITEM_TASK,
    queue="knowledge",
    max_retries=0,
    soft_time_limit=_EXTRACT_SOFT_TIME_LIMIT,
    time_limit=_EXTRACT_TIME_LIMIT,
)
def extract_knowledge_item(item_id: int) -> None:
    try:
        extract_knowledge_item_service(item_id)
    except SoftTimeLimitExceeded:
        # The service already failed the item under its own claim.
        logger.warning("Knowledge item %s extraction hit the time limit", item_id)


@shared_task(
    name=EXPAND_KNOWLEDGE_ARCHIVE_TASK,
    queue="knowledge",
    max_retries=0,
    soft_time_limit=_EXTRACT_SOFT_TIME_LIMIT,
    time_limit=_EXTRACT_TIME_LIMIT,
)
def expand_knowledge_archive(item_id: int) -> None:
    try:
        expand_knowledge_archive_service(item_id)
    except SoftTimeLimitExceeded:
        # The service already failed the archive under its own claim.
        logger.warning("Knowledge archive %s expansion hit the time limit", item_id)


@shared_task(
    name=COPY_PROJECT_KNOWLEDGE_TASK,
    queue="knowledge",
    max_retries=0,
    soft_time_limit=_COPY_SOFT_TIME_LIMIT,
    time_limit=_COPY_TIME_LIMIT,
)
def copy_project_knowledge(source_project_id: int, target_project_id: int) -> None:
    copied = copy_project_knowledge_service(
        source_project_id=source_project_id, target_project_id=target_project_id
    )
    logger.info(
        "Copied %s knowledge items from project %s to project %s",
        copied,
        source_project_id,
        target_project_id,
    )


@shared_task(
    name=SWEEP_KNOWLEDGE_STORAGE_TASK,
    # Beat (CELERY_BEAT_SCHEDULE), hourly.
    queue="knowledge",
    max_retries=0,
    soft_time_limit=_SWEEP_SOFT_TIME_LIMIT,
    time_limit=_SWEEP_TIME_LIMIT,
)
def sweep_knowledge_storage() -> None:
    blobs, files = sweep_knowledge_storage_service()
    logger.info("Knowledge storage sweep removed %s blobs and %s files", blobs, files)
