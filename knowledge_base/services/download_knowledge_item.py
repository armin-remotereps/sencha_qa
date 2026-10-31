"""Open a knowledge item's original file for download."""

from __future__ import annotations

from dataclasses import dataclass

from django.core.files import File

from knowledge_base.exceptions import KnowledgeItemNotFoundError
from knowledge_base.formats import matched_extension
from knowledge_base.models import KnowledgeItem
from knowledge_base.services.blob_storage import open_blob
from knowledge_base.services.item_processing import ITEM_NOT_FOUND_MESSAGE

FILE_MISSING_MESSAGE = "The stored file of this item is missing."


@dataclass(frozen=True)
class KnowledgeDownload:
    file: File[bytes]
    file_name: str
    size_bytes: int


def open_knowledge_download(*, project_id: int, item_id: int) -> KnowledgeDownload:
    """The item's file (any status); the caller closes it (FileResponse does)."""
    item = (
        KnowledgeItem.objects.select_related("blob")
        .only("name", "original_name", "size_bytes", "blob__storage_name")
        .filter(pk=item_id, project_id=project_id)
        .first()
    )
    if item is None:
        raise KnowledgeItemNotFoundError(ITEM_NOT_FOUND_MESSAGE)
    try:
        handle = open_blob(item.blob)
    except FileNotFoundError as exc:
        raise KnowledgeItemNotFoundError(FILE_MISSING_MESSAGE) from exc
    return KnowledgeDownload(
        file=handle,
        file_name=download_name(item.name, item.original_name),
        size_bytes=item.size_bytes,
    )


def download_name(name: str, original_name: str) -> str:
    """The item's name, with the original extension added back after a rename."""
    extension = matched_extension(original_name)
    if extension is None or name.lower().endswith(extension):
        return name
    return name + extension
