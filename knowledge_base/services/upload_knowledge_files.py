"""Store uploaded files as pending knowledge items and queue their extraction."""

from __future__ import annotations

from collections.abc import Sequence

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction

from accounts.models import CustomUser
from auto_tester.file_names import sanitize_file_name
from knowledge_base.archive_guard import format_size
from knowledge_base.exceptions import KnowledgeUploadInvalidError
from knowledge_base.formats import (
    SEVEN_ZIP_DISABLED_MESSAGE,
    SEVEN_ZIP_EXTENSION,
    kind_for_name,
    matched_extension,
)
from knowledge_base.models import (
    KNOWLEDGE_DESCRIPTION_MAX,
    KNOWLEDGE_NAME_MAX_LENGTH,
    KnowledgeBlob,
    KnowledgeItem,
    KnowledgeItemStatus,
    KnowledgeKind,
)
from knowledge_base.services.blob_storage import store_blob
from knowledge_base.services.item_processing import enqueue_item_processing
from knowledge_base.services.project_quota import (
    fits_quota,
    lock_project_usage,
    project_usage,
)
from knowledge_base.services.upload_budget import (
    release_upload_bytes,
    reserve_upload_bytes,
)
from knowledge_base.tags import merge_tags
from knowledge_base.types import RefusedFile, UploadOutcome

_QUOTA_MESSAGE = "the project's knowledge base storage limit would be exceeded"
_BUDGET_MESSAGE = (
    "too much was uploaded to this project in the last hour; try again later"
)


def _refusal_reason(upload_name: str, size: int) -> str | None:
    """Why this file cannot be stored (type, disabled 7z, size), or None.

    Archives have their own, higher size limit.
    """
    kind = kind_for_name(upload_name)
    if kind is None:
        return "this file type is not supported"
    if (
        matched_extension(upload_name) == SEVEN_ZIP_EXTENSION
        and not settings.KNOWLEDGE_BASE_ACCEPT_7Z
    ):
        return SEVEN_ZIP_DISABLED_MESSAGE
    max_bytes: int = (
        settings.KNOWLEDGE_BASE_MAX_ARCHIVE_BYTES
        if kind == KnowledgeKind.ARCHIVE
        else settings.KNOWLEDGE_BASE_MAX_FILE_BYTES
    )
    if size > max_bytes:
        return f"larger than {format_size(max_bytes)}"
    return None


def _refused(upload_name: str, reason: str | None) -> RefusedFile:
    return RefusedFile(
        name=sanitize_file_name(upload_name, fallback="file"),
        reason=reason or "this file type is not supported",
    )


def upload_knowledge_files(
    *,
    project_id: int,
    user: CustomUser,
    files: Sequence[UploadedFile],
    description: str,
    tags: Sequence[str] = (),
) -> UploadOutcome:
    """Store each acceptable file and create a pending item for it.

    A refused file (type, size, disabled 7z, quota) is listed in ``refused``
    and gets no item. Every item gets the normalised ``tags`` (an archive's
    files inherit them at expansion). Extraction is queued after commit.
    """
    if not files:
        raise KnowledgeUploadInvalidError("Choose at least one file to upload.")
    max_files: int = settings.KNOWLEDGE_BASE_MAX_FILES_PER_UPLOAD
    if len(files) > max_files:
        raise KnowledgeUploadInvalidError(
            f"Upload at most {max_files} files at a time."
        )
    clean_description = description.strip()[:KNOWLEDGE_DESCRIPTION_MAX]
    clean_tags = merge_tags(tags)
    refused: list[RefusedFile] = []
    accepted: list[tuple[UploadedFile, KnowledgeKind]] = []
    # Each accepted file has its bytes reserved in the hourly budget; the
    # finally gives back those of files that were never written (also when a
    # later reservation or write fails).
    stored: list[tuple[UploadedFile, KnowledgeKind, KnowledgeBlob]] = []
    try:
        # Unlocked pre-check, so a project at its quota writes nothing to
        # disk; the check under the project lock below is authoritative.
        planned = project_usage(project_id)
        for upload in files:
            upload_name = upload.name or ""
            size = upload.size or 0
            reason = _refusal_reason(upload_name, size)
            if reason is None and not fits_quota(planned, size):
                reason = _QUOTA_MESSAGE
            kind = kind_for_name(upload_name)
            if reason is None and kind is not None:
                if not reserve_upload_bytes(project_id, size):
                    reason = _BUDGET_MESSAGE
            if reason is not None or kind is None:
                refused.append(_refused(upload_name, reason))
                continue
            planned += size
            accepted.append((upload, kind))
        # Written to disk before the project row is locked, so a large upload
        # never holds up run starts or setup messages. A blob left unused by a
        # quota refusal below (a concurrent upload won the room) is removed by
        # the hourly sweep.
        for upload, accepted_kind in accepted:
            stored.append((upload, accepted_kind, store_blob(upload.chunks())))
    finally:
        for upload, _kind in accepted[len(stored) :]:
            release_upload_bytes(project_id, upload.size or 0)
    items: list[KnowledgeItem] = []
    with transaction.atomic():
        used = lock_project_usage(project_id)
        if used is None:
            raise KnowledgeUploadInvalidError("This project no longer exists.")
        for upload, item_kind, blob in stored:
            upload_name = upload.name or ""
            if not fits_quota(used, blob.size_bytes):
                refused.append(_refused(upload_name, _QUOTA_MESSAGE))
                continue
            used += blob.size_bytes
            items.append(
                KnowledgeItem(
                    project_id=project_id,
                    blob=blob,
                    uploaded_by=user,
                    name=sanitize_file_name(upload_name, fallback=f"file.{item_kind}"),
                    original_name=upload_name[:KNOWLEDGE_NAME_MAX_LENGTH],
                    kind=item_kind,
                    description=clean_description,
                    tags=list(clean_tags),
                    status=KnowledgeItemStatus.PENDING,
                    size_bytes=blob.size_bytes,
                )
            )
        created = KnowledgeItem.objects.bulk_create(items)
        for item in created:
            enqueue_item_processing(item.pk, item.kind)
    return UploadOutcome(
        created_ids=tuple(item.pk for item in created),
        refused=tuple(refused),
    )
