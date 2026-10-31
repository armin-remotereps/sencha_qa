"""Read one knowledge item's file into searchable chunks."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime

from celery.exceptions import SoftTimeLimitExceeded
from django.db import transaction

from knowledge_base.chunking import ChunkDraft, chunk_sections
from knowledge_base.exceptions import KnowledgeExtractionError
from knowledge_base.extraction import ExtractionOutput, extract_sections
from knowledge_base.models import (
    KNOWLEDGE_ERROR_MAX_LENGTH,
    KNOWLEDGE_EXTRACTOR_VERSION,
    KnowledgeChunk,
    KnowledgeItemStatus,
)
from knowledge_base.services.blob_storage import read_blob_bytes
from knowledge_base.services.item_claim import (
    TIMED_OUT_MESSAGE,
    claim_item,
    fail_claimed_item,
    lock_claimed_item,
)
from knowledge_base.services.item_processing import broadcast_item_state
from knowledge_base.services.search_vectors import write_search_vectors

logger = logging.getLogger(__name__)

CHUNK_BATCH_SIZE = 500
UNREADABLE_MESSAGE = "Could not read this file"


def extract_knowledge_item(item_id: int) -> None:
    """Claim the item, extract and chunk its file, index it, mark it ready.

    Expected failures fail the item with their safe message; anything else
    fails it with a generic message and is logged. A soft time limit fails
    it under this worker's claim and is re-raised to the task.
    """
    item = claim_item(item_id, archive=False)
    if item is None:
        return
    claimed_at = item.extract_started_at
    if claimed_at is None:  # always set by the claim; narrows the type
        return
    try:
        data = read_blob_bytes(item.blob)
        output = extract_sections(item.kind, data)
        del data
        drafts = chunk_sections(output.sections)
    except SoftTimeLimitExceeded:
        fail_claimed_item(item_id, claimed_at, TIMED_OUT_MESSAGE)
        raise
    except KnowledgeExtractionError as exc:
        fail_claimed_item(item_id, claimed_at, exc.message)
        return
    except Exception:
        # Never log extracted text: only the item id and the traceback.
        logger.exception("Knowledge item %s could not be extracted", item_id)
        fail_claimed_item(item_id, claimed_at, UNREADABLE_MESSAGE)
        return
    try:
        _save_chunks(item_id, claimed_at, output, drafts)
    except SoftTimeLimitExceeded:
        # The save rolled back; a large item can hit the limit while indexing.
        fail_claimed_item(item_id, claimed_at, TIMED_OUT_MESSAGE)
        raise


def _save_chunks(
    item_id: int,
    claimed_at: datetime,
    output: ExtractionOutput,
    drafts: Sequence[ChunkDraft],
) -> None:
    with transaction.atomic():
        item = lock_claimed_item(item_id, claimed_at)
        if item is None:
            # Retried, re-claimed or deleted meanwhile: drop this result.
            return
        KnowledgeChunk.objects.filter(item_id=item_id).delete()
        for start in range(0, len(drafts), CHUNK_BATCH_SIZE):
            KnowledgeChunk.objects.bulk_create(
                [
                    KnowledgeChunk(
                        item_id=item_id,
                        project_id=item.project_id,
                        ordinal=draft.ordinal,
                        page=draft.page,
                        location=draft.location,
                        heading=draft.heading,
                        text=draft.text,
                    )
                    for draft in drafts[start : start + CHUNK_BATCH_SIZE]
                ]
            )
        # Name and description read under the lock: a rename during the
        # extraction is not lost.
        write_search_vectors(item_id, item.name, item.description)
        item.status = KnowledgeItemStatus.READY
        item.error = ""
        item.warning = output.warning[:KNOWLEDGE_ERROR_MAX_LENGTH]
        item.char_count = output.char_count
        item.page_count = max((draft.page for draft in drafts), default=0)
        item.chunk_count = len(drafts)
        item.extractor_version = KNOWLEDGE_EXTRACTOR_VERSION
        item.save(
            update_fields=[
                "status",
                "error",
                "warning",
                "char_count",
                "page_count",
                "chunk_count",
                "extractor_version",
                "updated_at",
            ]
        )
        broadcast_item_state(item)
