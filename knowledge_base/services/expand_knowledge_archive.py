"""Read an archive item's members into pending child items."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.db import transaction

from auto_tester.file_names import sanitize_file_name
from knowledge_base.archive_readers import iter_archive_members
from knowledge_base.exceptions import KnowledgeExtractionError, UnsupportedContent
from knowledge_base.formats import (
    SEVEN_ZIP_DISABLED_MESSAGE,
    archive_format,
    kind_for_name,
)
from knowledge_base.models import (
    KNOWLEDGE_ERROR_MAX_LENGTH,
    KNOWLEDGE_EXTRACTOR_VERSION,
    KNOWLEDGE_NAME_MAX_LENGTH,
    KnowledgeBlob,
    KnowledgeItem,
    KnowledgeItemStatus,
    KnowledgeKind,
)
from knowledge_base.services.blob_storage import read_blob_bytes, store_blob_bytes
from knowledge_base.services.item_claim import (
    TIMED_OUT_MESSAGE,
    claim_item,
    fail_claimed_item,
    lock_claimed_item,
)
from knowledge_base.services.item_processing import (
    broadcast_item_state,
    enqueue_item_processing,
)
from knowledge_base.services.project_quota import (
    fits_quota,
    lock_project_usage,
    project_usage,
)
from knowledge_base.services.upload_budget import (
    release_upload_bytes,
    reserve_upload_bytes,
)

logger = logging.getLogger(__name__)

UNREADABLE_ARCHIVE_MESSAGE = "Could not read this archive"
UNSUPPORTED_ARCHIVE_MESSAGE = "This archive format is not supported"
_OVER_QUOTA_REASON = "storage limit reached"
_OVER_BUDGET_REASON = "hourly upload limit reached"


@dataclass(frozen=True)
class _StoredMember:
    label: str
    kind: KnowledgeKind
    blob: KnowledgeBlob


def expand_knowledge_archive(item_id: int) -> None:
    """Claim the archive, store its members as pending children, mark it ready.

    Whole-archive problems (limits, encryption) fail the archive and leave no
    children. Members over the project quota are skipped and listed in the
    archive's warning. Blobs stored for members that end up unused are left
    to the storage sweep.
    """
    item = claim_item(item_id, archive=True)
    if item is None:
        return
    claimed_at = item.extract_started_at
    if claimed_at is None:  # always set by the claim; narrows the type
        return
    skipped: list[str] = []
    try:
        fmt = archive_format(item.original_name) or archive_format(item.name)
        if fmt is None:
            raise UnsupportedContent(UNSUPPORTED_ARCHIVE_MESSAGE)
        if fmt == "7z" and not settings.KNOWLEDGE_BASE_ACCEPT_7Z:
            raise UnsupportedContent(SEVEN_ZIP_DISABLED_MESSAGE)
        data = read_blob_bytes(item.blob)
        members = _store_members(item.project_id, fmt, data, skipped)
        del data
        _create_children(item_id, claimed_at, members, skipped)
    except SoftTimeLimitExceeded:
        fail_claimed_item(item_id, claimed_at, TIMED_OUT_MESSAGE)
        raise
    except KnowledgeExtractionError as exc:
        fail_claimed_item(item_id, claimed_at, exc.message)
    except Exception:
        # Never log member contents: only the item id and the traceback.
        logger.exception("Knowledge archive %s could not be expanded", item_id)
        fail_claimed_item(item_id, claimed_at, UNREADABLE_ARCHIVE_MESSAGE)


def _store_members(
    project_id: int, fmt: str, data: bytes, skipped: list[str]
) -> list[_StoredMember]:
    """Store the accepted members' bytes (outside the project lock).

    Like an upload, each member must fit the quota (unlocked pre-check; the
    locked check in ``_create_children`` is authoritative) and the project's
    hourly upload budget before it is written, so a small, highly compressed
    archive cannot write past either.
    """
    stored: list[_StoredMember] = []
    planned = project_usage(project_id)
    for member in iter_archive_members(fmt, data, skipped):
        kind = kind_for_name(member.label)
        if kind is None or kind == KnowledgeKind.ARCHIVE:
            # The readers already skip these; kept as a guard.
            skipped.append(f"{member.label} (unsupported type)")
            continue
        size = len(member.data)
        if not fits_quota(planned, size):
            skipped.append(f"{member.label} ({_OVER_QUOTA_REASON})")
            continue
        if not reserve_upload_bytes(project_id, size):
            skipped.append(f"{member.label} ({_OVER_BUDGET_REASON})")
            continue
        try:
            blob = store_blob_bytes(member.data)
        except BaseException:
            release_upload_bytes(project_id, size)
            raise
        planned += size
        stored.append(_StoredMember(label=member.label, kind=kind, blob=blob))
    return stored


def _create_children(
    item_id: int,
    claimed_at: datetime,
    members: Sequence[_StoredMember],
    skipped: list[str],
) -> None:
    with transaction.atomic():
        archive = lock_claimed_item(item_id, claimed_at)
        if archive is None:
            # Retried, re-claimed or deleted meanwhile: drop this result.
            return
        used = lock_project_usage(archive.project_id)
        if used is None:
            return
        children: list[KnowledgeItem] = []
        for member in members:
            size = member.blob.size_bytes
            if not fits_quota(used, size):
                skipped.append(f"{member.label} ({_OVER_QUOTA_REASON})")
                continue
            used += size
            children.append(
                KnowledgeItem(
                    project_id=archive.project_id,
                    parent_id=archive.pk,
                    blob=member.blob,
                    uploaded_by_id=archive.uploaded_by_id,
                    name=sanitize_file_name(
                        member.label, fallback=f"file.{member.kind}"
                    ),
                    original_name=member.label[:KNOWLEDGE_NAME_MAX_LENGTH],
                    kind=member.kind,
                    status=KnowledgeItemStatus.PENDING,
                    size_bytes=size,
                )
            )
        created = KnowledgeItem.objects.bulk_create(children)
        archive.status = KnowledgeItemStatus.READY
        archive.error = ""
        archive.warning = skipped_warning(skipped)
        archive.char_count = 0
        archive.page_count = 0
        archive.chunk_count = 0
        archive.extractor_version = KNOWLEDGE_EXTRACTOR_VERSION
        archive.save(
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
        broadcast_item_state(archive)
        for child in created:
            enqueue_item_processing(child.pk, child.kind)


def skipped_warning(skipped: Sequence[str]) -> str:
    """ "Skipped: a (reason), b (reason) and N more", within the warning limit."""
    if not skipped:
        return ""
    prefix = "Skipped: "
    shown: list[str] = []
    for index, entry in enumerate(skipped):
        rest = len(skipped) - index - 1
        tail = f" and {rest} more" if rest else ""
        candidate = prefix + ", ".join([*shown, entry]) + tail
        if len(candidate) > KNOWLEDGE_ERROR_MAX_LENGTH:
            break
        shown.append(entry)
    remaining = len(skipped) - len(shown)
    if not shown:
        return f"Skipped {len(skipped)} entries."[:KNOWLEDGE_ERROR_MAX_LENGTH]
    text = prefix + ", ".join(shown)
    if remaining:
        text += f" and {remaining} more"
    return text[:KNOWLEDGE_ERROR_MAX_LENGTH]
